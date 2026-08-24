"""
F0 数据管道 — 管道编排器 (Pipeline)

这是 ZiZu 的心脏。
每一条 MQTT 消息都流经此管道:
  RawMessage → [Hook1 解析] → [Hook2 归一化] → [Hook3 存储] → [CE 透传骨架]

CE 三条路径 (方案B):
  Path A: SymPy 公式计算 → F1 激活时工作, 当前 no-op
  Path B: CAGG 窗口聚合   → TSDB 内置, 零 Python 代码
  Path C: 跨节点 SQL 聚合   → APScheduler Job, 当前 no-op

设计原则:
  - 单线程异步 (asyncio), 无锁
  - 每个 Hook 可独立插拔
  - metrics 全程可观测
  - 异常不传播: Hook 失败跳过, 记录日志, 继续下一条
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from uuid import UUID

from loguru import logger

from app.core.config import settings
from app.models.schemas import (
    NormalizedMessage,
    ParsedMessage,
    PipelineMetrics,
    PipelineStatus,
    RawMessage,
)
from app.services.mqtt_client import MqttClient
from app.services.normalizer import TagNormalizationRule, normalize
from app.services.parser import parse_neuron_json
from app.services.alarm_runtime import AlarmRuntime
from app.services.data_trunk import DataTrunk, RawObservationAdapter, TagMetadata
from app.services.data_trunk_contracts import (
    DataTrunkError,
    RawObservation,
    ValueKind,
)
from app.services.data_trunk_postgres import build_postgres_data_trunk
from app.services.telemetry_store import TelemetryRecord
from app.services.tag_mqtt_alarm_adapter import (
    ERROR_LEVELS,
    InMemoryTagAlarmSourceResolver,
    MqttAlarmAdapter,
    TagAlarmAdapter,
    TagAlarmSample,
    TagAlarmSource,
)


MAX_INGEST_ATTEMPTS = 5
INGEST_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0, 4.0)


class DataPipeline:
    """
    F0 数据管道。

    用法:
        pipeline = DataPipeline()
        await pipeline.start()     # 启动 MQTT + 加载规则
        ...                        # 自动消费消息
        await pipeline.stop()      # 优雅停更
    """

    def __init__(self, *, data_trunk: DataTrunk | None = None) -> None:
        # ---- 组件 ----
        self._mqtt: MqttClient | None = None
        self._rules: dict[str, TagNormalizationRule] = {}  # {tag_name: rule}
        self._node_id_map: dict[str, UUID] = {}            # {node_name: node_id}
        self._tag_id_map: dict[str, UUID] = {}             # {tag_name: tag_id}
        self._tag_alarm_sources = InMemoryTagAlarmSourceResolver()
        self._mqtt_alarm_tag_ids: dict[str, UUID] = {}
        self._tag_alarm_adapter: TagAlarmAdapter | None = None
        self._mqtt_alarm_adapter: MqttAlarmAdapter | None = None
        self._entity_alarm_adapter = None
        # Neuron 点位按 source_path 精确映射: {(neuron_node, group, tag_name): (node_id, tag_id, rule)}
        self._neuron_tag_map: dict[tuple[str, str, str], tuple[UUID, UUID, TagNormalizationRule]] = {}
        self._raw_neuron_tag_map: dict[tuple[str, str, str], TagMetadata] = {}
        self._raw_node_tag_map: dict[tuple[str, str], TagMetadata] = {}

        # ---- Metrics ----
        self.metrics = PipelineMetrics()
        self._started_at: datetime | None = None

        # ---- 批量写入缓冲 ----
        self._data_trunk = data_trunk or build_postgres_data_trunk()
        self._raw_observation_adapter = RawObservationAdapter()
        self._buffer: list[RawObservation] = []
        self._legacy_projections: dict[UUID, TelemetryRecord] = {}
        self._buffer_lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()  # 串行化 flush，避免连接池耗尽
        self._flush_event = asyncio.Event()  # 缓冲区满或停更时唤醒 flush
        self._stop_event = asyncio.Event()
        self._flush_task: asyncio.Task | None = None
        self._retry_prefix_ids: tuple[UUID, ...] = ()
        self._retry_attempts = 0
        self._retry_error_code = "DATA_TRUNK_UNAVAILABLE"

        # ---- tag 规则动态重载 ----
        self._reload_task: asyncio.Task | None = None

    # ══════════════════════════════
    # 生命周期
    # ══════════════════════════════

    async def start(self) -> None:
        """启动管道。"""
        logger.info("[Pipeline] Starting F0 data pipeline ...")
        self.metrics.status = PipelineStatus.STARTING
        self._started_at = datetime.now(timezone.utc)

        # Step 1: 初始化 DB 连接池
        from app.services.telemetry_store import init_db_pool

        init_db_pool(
            min_conn=settings.db_pool_min,
            max_conn=settings.db_pool_max,
        )

        # Step 2: 加载归一化规则和 ID 映射
        await self._load_tag_rules()

        # Step 3: 启动 MQTT 客户端
        self._mqtt = MqttClient(on_message_callback=self.on_message)
        await self._mqtt.start()

        # Step 4: 启动批量写入 flush 后台任务
        self._flush_task = asyncio.create_task(self._flush_loop())

        # Step 5: 启动 tag 规则动态重载任务
        self._reload_task = asyncio.create_task(self._periodic_reload_rules())

        self.metrics.status = PipelineStatus.RUNNING
        logger.success(
            "[Pipeline] F0 pipeline running ✅  rules={}, nodes={}, tags={}",
            len(self._rules),
            len(self._node_id_map),
            len(self._tag_id_map),
        )

    async def reload_mqtt_topics(self) -> None:
        """运行时根据 settings 重新订阅 MQTT topic。"""
        if self._mqtt is not None:
            self._mqtt.resubscribe(settings.mqtt_telemetry_topics)
            logger.info("[Pipeline] MQTT topics reloaded: {}", settings.mqtt_telemetry_topics)

    async def stop(self, *, close_database: bool = True) -> None:
        """优雅停止。"""
        logger.info("[Pipeline] Stopping F0 data pipeline ...")
        self.metrics.status = PipelineStatus.STOPPING

        # 停止 reload task
        if self._reload_task and not self._reload_task.done():
            self._reload_task.cancel()
            try:
                await self._reload_task
            except asyncio.CancelledError:
                pass

        # 停止 flush task
        self._stop_event.set()
        self._flush_event.set()
        if self._flush_task and not self._flush_task.done():
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # 最后一次 flush
        async with self._flush_lock:
            await self._do_flush()

        # 断开 MQTT
        if self._mqtt:
            await self._mqtt.stop()

        # 关闭连接池
        if close_database:
            from app.services.telemetry_store import close_db_pool

            close_db_pool()

        self.metrics.status = PipelineStatus.STOPPED
        logger.info("[Pipeline] F0 pipeline stopped")

    # ══════════════════════════════
    # 核心处理函数 — 每条消息的入口
    # ══════════════════════════════

    async def on_message(self, mqtt_msg) -> None:
        """
        MQTT 消息回调 → 管道入口。

        整个 F0 的消息流转在此函数中完成。
        """
        self.metrics.messages_received += 1
        self.metrics.last_message_at = datetime.now(timezone.utc)

        # 告警 topic 只构造统一观测；生命周期由 AlarmRuntime 独占。
        if self._mqtt is not None and self._mqtt.is_alarm_topic(mqtt_msg.topic):
            try:
                outcomes = await asyncio.to_thread(
                    self._submit_mqtt_alarm_observations,
                    mqtt_msg.topic,
                    mqtt_msg.payload,
                    datetime.now(timezone.utc),
                )
                logger.debug("[Pipeline] MQTT alarm observations submitted: {}", len(outcomes))
            except Exception as e:
                logger.error("[Pipeline] MQTT alarm adaptation failed: {}", e)
            return

        raw = RawMessage(
            topic=mqtt_msg.topic,
            payload=mqtt_msg.payload,
            qos=mqtt_msg.qos,
        )

        # ── Hook 1: 解析 (~30 行) ──
        try:
            parsed = await asyncio.to_thread(parse_neuron_json, raw)
        except Exception as e:
            logger.error("[Pipeline] Parser exception on topic={}: {}", raw.topic, e)
            self.metrics.messages_parse_error += 1
            return
        if parsed is None:
            self.metrics.messages_parse_error += 1
            return  # 跳过无法解析的消息
        self.metrics.messages_parsed_ok += 1

        source_sequence = getattr(mqtt_msg, "sequence", None)
        if not isinstance(source_sequence, int) or isinstance(source_sequence, bool):
            source_sequence = None
        raw_observations = self._raw_observation_adapter.from_parsed(
            parsed,
            self._raw_tag_catalog(parsed),
            received_at=raw.timestamp_recv,
            source_message_id=hashlib.sha256(raw.payload).hexdigest(),
            source_sequence=source_sequence,
        )

        # 从普通 telemetry 消息中提取 error1/error2/error3 分组告警
        if self._mqtt is not None and not self._mqtt.is_alarm_topic(raw.topic):
            if any(k in ERROR_LEVELS for k in parsed.tags):
                try:
                    outcomes = await asyncio.to_thread(
                        self._submit_mqtt_alarm_observations,
                        raw.topic,
                        raw.payload,
                        parsed.ts,
                    )
                    logger.debug(
                        "[Pipeline] MQTT observations extracted from {}: {}",
                        raw.topic,
                        len(outcomes),
                    )
                except Exception as e:
                    logger.error("[Pipeline] MQTT alarm extraction failed: {}", e)

        # ── Hook 2: 归一化 (CPU 密集型，放到线程池避免阻塞事件循环) ──
        # 复制 rules 引用避免 reload 期间的竞态；dict 引用替换是原子的。
        rules_snapshot = self._rules
        normalized = await asyncio.to_thread(normalize, parsed, rules=rules_snapshot)
        self.metrics.points_normalized += normalized.point_count

        # 填充 node_id (延迟解析时保留的 node_name 需要映射回 node_id)
        for point in normalized.points:
            point.node_name = parsed.node_name

        # ── Hook 3: 持久化 (缓冲写入) (~30 行) ──
        legacy_by_tag = {item.tag_id: item for item in self._to_records(normalized)}
        async with self._buffer_lock:
            self._buffer.extend(raw_observations)
            for observation in raw_observations:
                projection = legacy_by_tag.get(observation.tag_id)
                if projection is not None:
                    self._legacy_projections[observation.observation_id] = projection
            if len(self._buffer) >= settings.pipeline_batch_size:
                self._flush_event.set()

        # ══════════════════════════════════════
        # CE 三条路径 (按需激活, F0 阶段全部透传)
        # ══════════════════════════════════════

        # ── CE Path A: SymPy 公式计算 (F1 核心) ──
        # 当前: no-op（无公式注册）
        # F1 激活后: dispatch_logical_triggers(normalized)
        # await self._ce_path_a_formula(normalized)

        # ── CE Path B: CAGG 窗口聚合 (TSDB 内置) ──
        # 当前: 已在 SQL 层自动运行 (CREATE MATERIALIZED VIEW WITH continuous)
        # 无需任何 Python 代码干预

        # ── CE Path C: 跨节点 SQL 聚合 (F3 汇总) ──
        # 当前: no-op（无节点树）
        # F3 激活后: APScheduler Job 每 10s 执行 GROUP BY

    # ══════════════════════════════
    # CE 路径预留 (F1/F3 激活后实现)
    # ══════════════════════════════

    async def _ce_path_a_formula(self, normalized: NormalizedMessage) -> None:
        """
        CE Path A: SymPy 公式计算 (F1 VirtualPointEngine)。

        触发条件: 变化的 tag 是某个 LogicalTag formula 的 source。
        实现: 在 Phase 2 S6 中补全。
        """
        # TODO Phase 2 S6:
        # from app.services.virtual_point_engine import VirtualPointEngine
        # virtual_points = await VirtualPointEngine.instance().evaluate(normalized)
        # if virtual_points:
        #     records = self._to_records_from_virtual(virtual_points)
        #     async with self._buffer_lock:
        #         self._buffer.extend(records)
        pass

    # ══════════════════════════════
    # 辅助方法
    # ══════════════════════════════

    def _to_records(self, msg: NormalizedMessage) -> list[TelemetryRecord]:
        """NormalizedMessage → TelemetryRecord[] (需要 ID 映射)."""
        records: list[TelemetryRecord] = []
        for point in msg.points:
            nid: UUID | None = None
            tid: UUID | None = None

            # 优先按 Neuron source_path 精确匹配 (neuron_node/group/tag_name)
            if point.group:
                key = (point.node_name or msg.source_node, point.group, point.tag_name)
                mapped = self._neuron_tag_map.get(key)
                if mapped:
                    nid, tid, _rule = mapped

            # 回退：按 node_name + tag_name 全局匹配 (兼容 telemetry/# 格式)
            if nid is None or tid is None:
                nid = self._node_id_map.get(point.node_name or msg.source_node)
                tid = self._tag_id_map.get(point.tag_name)

            if nid is not None and tid is not None:
                records.append(TelemetryRecord.from_point(point, nid, tid))
            else:
                logger.debug(
                    "[Pipeline] Unresolved: node={} group={} tag={}",
                    point.node_name or msg.source_node,
                    point.group,
                    point.tag_name,
                )
        return records

    def _raw_tag_catalog(self, parsed: ParsedMessage) -> dict[str, TagMetadata]:
        """Resolve parsed names to exact physical identities before buffering L0."""
        catalog: dict[str, TagMetadata] = {}
        for raw_name in parsed.tags:
            exact = None
            if parsed.group:
                exact = self._raw_neuron_tag_map.get(
                    (parsed.node_name, parsed.group, raw_name)
                )
            exact = exact or self._raw_node_tag_map.get((parsed.node_name, raw_name))
            if exact is not None:
                catalog[raw_name] = exact
                continue
            rule = self._rules.get(raw_name)
            mapped = None
            if parsed.group:
                mapped = self._neuron_tag_map.get(
                    (parsed.node_name, parsed.group, raw_name)
                )
            if mapped is not None:
                node_id, tag_id, mapped_rule = mapped
                rule = mapped_rule
                stable_key = f"{parsed.node_name}/{parsed.group}/{raw_name}"
            else:
                continue
            if node_id is None or tag_id is None or rule is None:
                continue
            data_type = getattr(rule.data_type, "value", rule.data_type)
            catalog[raw_name] = TagMetadata(
                node_id=node_id,
                tag_id=tag_id,
                stable_source_key=stable_key,
                data_type=str(data_type),
                unit=rule.unit_from,
                timestamp_trusted=False,
            )
        return catalog

    async def _load_tag_rules(self) -> None:
        """从 t_tags 表加载归一化规则和 ID 映射。

        新规则先在本地 dict 构建，最后原子替换，避免重载期间 on_message 读到中间状态。
        DB 查询部分是同步阻塞的，在线程池中执行。
        """
        try:
            from app.services.telemetry_store import get_connection

            def _fetch() -> list:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT t.name AS tag_name,
                                   n.name AS node_name,
                                   t.id AS tag_id,
                                   n.id AS node_id,
                                   t.data_type,
                                   t.scale_factor,
                                   t.value_offset,
                                   t.unit_from,
                                   t.unit_to,
                                   t.range_min,
                                   t.range_max,
                                   t.source_type,
                                   t.source_path,
                                   n.source_catalog_key,
                                   t.timestamp_trusted
                            FROM t_tags t
                            JOIN t_nodes n ON t.node_id = n.id
                            WHERE t.enabled = true AND n.enabled = true;
                        """)
                        return cur.fetchall()

            rows = await asyncio.to_thread(_fetch)

            new_rules: dict[str, TagNormalizationRule] = {}
            new_node_id_map: dict[str, UUID] = {}
            new_tag_id_map: dict[str, UUID] = {}
            new_neuron_tag_map: dict[tuple[str, str, str], tuple[UUID, UUID, TagNormalizationRule]] = {}
            new_raw_neuron_tag_map: dict[tuple[str, str, str], TagMetadata] = {}
            new_raw_node_tag_map: dict[tuple[str, str], TagMetadata] = {}

            for row in rows:
                (tag_name, node_name, tag_id, node_id, data_type,
                 scale_factor, offset, unit_from, unit_to, range_min, range_max,
                 source_type, source_path, node_source_catalog_key,
                 timestamp_trusted) = row

                rule = TagNormalizationRule(
                    tag_name=tag_name,
                    data_type=data_type,
                    scale_factor=scale_factor or 1.0,
                    offset=offset or 0.0,
                    unit_from=unit_from,
                    unit_to=unit_to,
                    range_min=range_min,
                    range_max=range_max,
                )
                new_rules[tag_name] = rule
                new_node_id_map[node_name] = node_id
                new_tag_id_map[tag_name] = tag_id
                raw_metadata = TagMetadata(
                    node_id=node_id,
                    tag_id=tag_id,
                    stable_source_key=(
                        source_path
                        or f"{node_source_catalog_key or node_name}/{tag_name}"
                    ),
                    data_type=str(getattr(data_type, "value", data_type)),
                    unit=unit_from or unit_to,
                    timestamp_trusted=bool(timestamp_trusted),
                )
                new_raw_node_tag_map[(node_name, tag_name)] = raw_metadata

                if source_type == "neuron" and source_path:
                    parts = source_path.split("/")
                    if len(parts) >= 3:
                        neuron_node, neuron_group = parts[0], parts[1]
                        neuron_tag_name = "/".join(parts[2:])
                        new_neuron_tag_map[(neuron_node, neuron_group, neuron_tag_name)] = (
                            node_id, tag_id, rule
                        )
                        new_raw_neuron_tag_map[
                            (neuron_node, neuron_group, neuron_tag_name)
                        ] = raw_metadata
                        # 归一化规则同时按 Neuron tag 名索引，保证 normalizer 能找到
                        new_rules[neuron_tag_name] = rule

            # 原子替换
            self._rules = new_rules
            self._node_id_map = new_node_id_map
            self._tag_id_map = new_tag_id_map
            self._neuron_tag_map = new_neuron_tag_map
            self._raw_neuron_tag_map = new_raw_neuron_tag_map
            self._raw_node_tag_map = new_raw_node_tag_map

            # 仅加载已确认实体实例的活动物理来源。重名 MQTT 外部 ID 不猜测
            # 映射，必须先通过解决方案绑定/命名消歧后才进入统一告警。
            def _fetch_alarm_sources() -> tuple[dict[UUID, TagAlarmSource], dict[str, UUID]]:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT binding.tag_id, binding.entity_instance_id,
                                   tag.name, instance.freshness_seconds
                            FROM t_entity_instance_bindings binding
                            JOIN t_entity_instances instance
                              ON instance.id = binding.entity_instance_id
                            JOIN t_device_instances device
                              ON device.id = instance.device_instance_id
                            JOIN t_tags tag ON tag.id = binding.tag_id
                            WHERE binding.active = TRUE
                              AND instance.active = TRUE
                              AND device.active = TRUE
                              AND tag.enabled = TRUE
                        """)
                        sources: dict[UUID, TagAlarmSource] = {}
                        by_name: dict[str, list[UUID]] = {}
                        for row in cur.fetchall():
                            tag_id, entity_instance_id, tag_name, freshness_seconds = row
                            sources[tag_id] = TagAlarmSource(
                                tag_id=tag_id,
                                entity_instance_id=entity_instance_id,
                                tag_name=tag_name,
                                max_observation_gap_seconds=freshness_seconds,
                            )
                            by_name.setdefault(tag_name, []).append(tag_id)
                return (
                    sources,
                    {
                        tag_name: tag_ids[0]
                        for tag_name, tag_ids in by_name.items()
                        if len(tag_ids) == 1
                    },
                )

            alarm_sources, mqtt_tag_ids = await asyncio.to_thread(_fetch_alarm_sources)
            self._tag_alarm_sources.replace(alarm_sources)
            self._mqtt_alarm_tag_ids = mqtt_tag_ids
            if self._mqtt_alarm_adapter is not None:
                self._mqtt_alarm_adapter.replace_tag_ids(mqtt_tag_ids)
            logger.info(
                "[Pipeline] Loaded {} tag rules, {} neuron source-path mappings, {} unified alarm sources",
                len(self._rules), len(self._neuron_tag_map), len(alarm_sources),
            )

        except Exception as e:
            logger.warning("[Pipeline] Failed to load tag rules: {}", e)

    async def reload_rules_now(self) -> None:
        """立即重载 tag 规则和告警配置 (供 API 调用)。"""
        try:
            await self._load_tag_rules()
            logger.info("[Pipeline] Rules reloaded on demand")
        except Exception as e:
            logger.warning("[Pipeline] On-demand reload failed: {}", e)

    async def flush_now(self) -> None:
        """Flush buffered protocol observations through the production store.

        Besides controlled shutdown, this is the public seam used by protocol
        simulator acceptance tests: they publish a real Neuron MQTT-shaped
        message through ``on_message`` and then wait for durable visibility.
        """
        async with self._flush_lock:
            await self._do_flush()

    def buffer_observation_ids(self) -> tuple[UUID, ...]:
        """Return the immutable retry prefix identity for diagnostics/tests."""
        return tuple(item.observation_id for item in self._buffer)

    def buffer_sequences(self) -> tuple[int | None, ...]:
        return tuple(item.source_sequence for item in self._buffer)

    async def _periodic_reload_rules(self) -> None:
        """定时重载 tag 规则，让新导入点位无需重启即可生效。"""
        while True:
            await asyncio.sleep(settings.pipeline_reload_rules_interval_sec)
            try:
                await self._load_tag_rules()
            except Exception as e:
                logger.warning("[Pipeline] Periodic reload rules failed: {}", e)

    async def _flush_loop(self) -> None:
        """后台 flush 循环：缓冲区满或超时则写入 DB。"""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._flush_event.wait(),
                    timeout=settings.pipeline_flush_interval_sec,
                )
            except asyncio.TimeoutError:
                pass
            self._flush_event.clear()
            if self._buffer:
                async with self._flush_lock:
                    await self._do_flush()

    async def _do_flush(self) -> None:
        """Persist a stable prefix and remove it only after a commit receipt."""
        async with self._buffer_lock:
            batch = tuple(self._buffer[: settings.pipeline_batch_size])
        if not batch:
            return
        prefix_ids = tuple(item.observation_id for item in batch)
        if prefix_ids != self._retry_prefix_ids:
            self._retry_prefix_ids = prefix_ids
            self._retry_attempts = 0
            self._retry_error_code = "DATA_TRUNK_UNAVAILABLE"
        if self._retry_attempts >= MAX_INGEST_ATTEMPTS:
            await self._record_terminal_failure(batch)
            return
        try:
            receipt = await asyncio.to_thread(self._data_trunk.ingest, batch)
        except DataTrunkError as e:
            self.metrics.db_write_errors += 1
            self._retry_attempts += 1
            self._retry_error_code = e.code
            logger.error(
                "[Pipeline] Data trunk write error ({}/{}; {} observations): {}",
                self._retry_attempts,
                MAX_INGEST_ATTEMPTS,
                len(batch),
                e.code,
            )
            if self._retry_attempts >= MAX_INGEST_ATTEMPTS:
                await self._record_terminal_failure(batch)
            else:
                await asyncio.sleep(INGEST_RETRY_DELAYS[self._retry_attempts - 1])
            return

        async with self._buffer_lock:
            committed_ids = tuple(item.observation_id for item in batch)
            current_ids = tuple(
                item.observation_id for item in self._buffer[: len(batch)]
            )
            if current_ids != committed_ids:
                raise RuntimeError("pipeline buffer prefix changed during ingest")
            del self._buffer[: len(batch)]
            accepted_ids = set(receipt.accepted_l0_observation_ids)
            if not accepted_ids and receipt.accepted_l0_count == len(batch):
                accepted_ids = set(committed_ids)
            compatibility_batch = [
                self._legacy_projections.pop(
                    item.observation_id,
                    _legacy_record(item),
                )
                for item in batch
                if item.observation_id in accepted_ids
            ]
            for item in batch:
                self._legacy_projections.pop(item.observation_id, None)
        self._reset_retry_state()
        self.metrics.points_written_db += receipt.accepted_l0_count

        try:
            if compatibility_batch:
                await asyncio.to_thread(
                    self._submit_unified_tag_alarms,
                    compatibility_batch,
                )
                await asyncio.to_thread(
                    self._submit_installed_entity_alarms,
                    self._entity_instances_covered_by_tag_batch(compatibility_batch),
                )
        except Exception as e:
            logger.error("[Pipeline] Unified alarm processing failed: {}", e)

    async def _record_terminal_failure(
        self,
        batch: tuple[RawObservation, ...],
    ) -> None:
        try:
            failure_reference = await asyncio.to_thread(
                self._data_trunk.record_failure,
                batch,
                attempts=self._retry_attempts,
                error_code=self._retry_error_code,
            )
        except DataTrunkError as exc:
            self.metrics.db_write_errors += 1
            logger.error(
                "[Pipeline] Failure reference unavailable for {} observations: {}",
                len(batch),
                exc.code,
            )
            return
        async with self._buffer_lock:
            committed_ids = tuple(item.observation_id for item in batch)
            current_ids = tuple(
                item.observation_id for item in self._buffer[: len(batch)]
            )
            if current_ids != committed_ids:
                raise RuntimeError("pipeline buffer prefix changed during failure record")
            del self._buffer[: len(batch)]
            for item in batch:
                self._legacy_projections.pop(item.observation_id, None)
        logger.error(
            "[Pipeline] Terminal ingestion failure recorded: {}",
            failure_reference,
        )
        self._reset_retry_state()

    def _reset_retry_state(self) -> None:
        self._retry_prefix_ids = ()
        self._retry_attempts = 0
        self._retry_error_code = "DATA_TRUNK_UNAVAILABLE"

    def _entity_instances_covered_by_tag_batch(
        self,
        batch: list[TelemetryRecord],
    ) -> set[UUID]:
        """Keep each confirmed physical source on exactly one lifecycle path per flush."""
        return {
            source.entity_instance_id
            for record in batch
            if _telemetry_value(record) is not None
            and (source := self._tag_alarm_sources.resolve(record.tag_id)) is not None
        }

    def _submit_installed_entity_alarms(
        self,
        excluded_entity_instance_ids: set[UUID],
    ) -> None:
        """Keep entity freshness and quality for installed sources absent from this batch."""
        if self._entity_alarm_adapter is None:
            from app.services.entity_alarm_adapter import (
                build_postgres_entity_alarm_adapter,
            )

            self._entity_alarm_adapter = build_postgres_entity_alarm_adapter()
        outcomes = self._entity_alarm_adapter.submit_all(
            exclude_entity_instance_ids=excluded_entity_instance_ids,
        )
        if outcomes:
            logger.debug(
                "[Pipeline] Installed entity observations submitted: {}",
                len(outcomes),
            )

    def _tag_alarm_adapter_for_runtime(self) -> TagAlarmAdapter:
        if self._tag_alarm_adapter is None:
            from app.services.alarm_postgres import (
                PostgresAlarmDefinitionCatalog,
                PostgresAlarmRepository,
            )

            definitions = PostgresAlarmDefinitionCatalog()
            self._tag_alarm_adapter = TagAlarmAdapter(
                definitions,
                AlarmRuntime(definitions, PostgresAlarmRepository()),
                self._tag_alarm_sources,
            )
        return self._tag_alarm_adapter

    def _submit_unified_tag_alarms(self, batch: list[TelemetryRecord]) -> None:
        """Submit only durable, uniquely confirmed tag observations to ADR-0004."""
        adapter = self._tag_alarm_adapter_for_runtime()
        outcomes = []
        for record in sorted(batch, key=lambda item: item.ts):
            value = _telemetry_value(record)
            if value is None:
                continue
            outcomes.extend(
                adapter.submit(
                    TagAlarmSample(
                        record.tag_id,
                        record.ts,
                        value,
                        record.quality,
                    )
                )
            )
        if outcomes:
            logger.debug(
                "[Pipeline] Unified tag observations submitted: {}",
                len(outcomes),
            )

    def _submit_mqtt_alarm_observations(
        self,
        topic: str,
        payload: bytes,
        observed_at: datetime,
    ) -> tuple:
        if self._mqtt_alarm_adapter is None:
            self._mqtt_alarm_adapter = MqttAlarmAdapter(
                self._tag_alarm_adapter_for_runtime(),
                self._mqtt_alarm_tag_ids,
            )
        return self._mqtt_alarm_adapter.submit(topic, payload, observed_at)

    @property
    def uptime_seconds(self) -> float:
        if self._started_at:
            return (datetime.now(timezone.utc) - self._started_at).total_seconds()
        return 0.0


def _telemetry_value(record: TelemetryRecord):
    for name in ("value_str", "value_bool", "value_int", "value_float"):
        value = getattr(record, name)
        if value is not None:
            return value
    return None


def _legacy_record(observation: RawObservation) -> TelemetryRecord:
    """Project committed L0 into the legacy alarm reader without a second DB write."""
    fields: dict[str, object] = {}
    if observation.value.kind is ValueKind.FLOAT:
        fields["value_float"] = float(observation.value.value)
    elif observation.value.kind is ValueKind.INT:
        fields["value_int"] = int(observation.value.value)
    elif observation.value.kind is ValueKind.BOOL:
        fields["value_bool"] = bool(observation.value.value)
    elif observation.value.kind in {ValueKind.STRING, ValueKind.ENUM}:
        fields["value_str"] = str(observation.value.value)
    else:
        raise DataTrunkError(
            "RAW_OBSERVATION_INVALID",
            "Committed L0 observation cannot be projected to legacy telemetry",
        )
    return TelemetryRecord(
        ts=observation.source_timestamp,
        node_id=observation.node_id,
        tag_id=observation.tag_id,
        quality=int(observation.quality),
        **fields,
    )
