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
from app.services.alarm_processor import process_alarm_message, ERROR_LEVELS
from app.services.telemetry_store import batch_insert_telemetry, upsert_telemetry_latest, TelemetryRecord
from app.services.tag_alarm_engine import process_tag_alarms
from app.services.entity_alarm_engine import process_entity_alarms


class DataPipeline:
    """
    F0 数据管道。

    用法:
        pipeline = DataPipeline()
        await pipeline.start()     # 启动 MQTT + 加载规则
        ...                        # 自动消费消息
        await pipeline.stop()      # 优雅停更
    """

    def __init__(self) -> None:
        # ---- 组件 ----
        self._mqtt: MqttClient | None = None
        self._rules: dict[str, TagNormalizationRule] = {}  # {tag_name: rule}
        self._node_id_map: dict[str, UUID] = {}            # {node_name: node_id}
        self._tag_id_map: dict[str, UUID] = {}             # {tag_name: tag_id}
        self._alarm_tag_map: dict[str, dict] = {}          # {tag_id(str): alarm meta}
        self._alarm_name_map: dict[str, dict] = {}         # {tag_name(str): alarm meta} for MQTT path
        self._entity_alarm_index: dict[str, list[dict]] = {} # {tag_id(str): [entity alarm bindings]}
        # Neuron 点位按 source_path 精确映射: {(neuron_node, group, tag_name): (node_id, tag_id, rule)}
        self._neuron_tag_map: dict[tuple[str, str, str], tuple[UUID, UUID, TagNormalizationRule]] = {}

        # ---- Metrics ----
        self.metrics = PipelineMetrics()
        self._started_at: datetime | None = None

        # ---- 批量写入缓冲 ----
        self._buffer: list[TelemetryRecord] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()  # 串行化 flush，避免连接池耗尽
        self._flush_event = asyncio.Event()  # 缓冲区满或停更时唤醒 flush
        self._stop_event = asyncio.Event()
        self._flush_task: asyncio.Task | None = None

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

    async def stop(self) -> None:
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

        # 路由：告警 topic 直接走告警处理器
        if self._mqtt is not None and self._mqtt.is_alarm_topic(mqtt_msg.topic):
            try:
                result = await asyncio.to_thread(process_alarm_message, mqtt_msg.topic, mqtt_msg.payload, self._alarm_name_map)
                logger.debug("[Pipeline] Alarm message processed: {}", result)
            except Exception as e:
                logger.error("[Pipeline] Alarm processing failed: {}", e)
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

        # 从普通 telemetry 消息中提取 error1/error2/error3 分组告警
        if self._mqtt is not None and not self._mqtt.is_alarm_topic(raw.topic):
            if any(k in ERROR_LEVELS for k in parsed.tags):
                try:
                    result = await asyncio.to_thread(process_alarm_message, raw.topic, raw.payload, self._alarm_name_map)
                    logger.debug('[Pipeline] Extracted alarms from telemetry topic {}: {}', raw.topic, result)
                except Exception as e:
                    logger.error('[Pipeline] Alarm extraction failed: {}', e)

        # ── Hook 2: 归一化 (CPU 密集型，放到线程池避免阻塞事件循环) ──
        # 复制 rules 引用避免 reload 期间的竞态；dict 引用替换是原子的。
        rules_snapshot = self._rules
        normalized = await asyncio.to_thread(normalize, parsed, rules=rules_snapshot)
        self.metrics.points_normalized += normalized.point_count

        # 填充 node_id (延迟解析时保留的 node_name 需要映射回 node_id)
        for point in normalized.points:
            point.node_name = parsed.node_name

        # ── Hook 3: 持久化 (缓冲写入) (~30 行) ──
        records = self._to_records(normalized)
        should_flush = False
        if records and self._alarm_tag_map:
            try:
                alarm_result = await asyncio.to_thread(
                    process_tag_alarms,
                    [r.model_dump() for r in records],
                    self._alarm_tag_map,
                )
                logger.debug("[Pipeline] Tag alarms processed: {}", alarm_result)
            except Exception as e:
                logger.error("[Pipeline] Tag alarm processing failed: {}", e)
        

        if records and self._entity_alarm_index:
            try:
                entity_alarm_result = await asyncio.to_thread(
                    process_entity_alarms,
                    [r.model_dump() for r in records],
                    self._entity_alarm_index,
                )
                logger.debug("[Pipeline] Entity alarms processed: {}", entity_alarm_result)
            except Exception as e:
                logger.error("[Pipeline] Entity alarm processing failed: {}", e)

        async with self._buffer_lock:
            self._buffer.extend(records)
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
                                   t.source_path
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

            for row in rows:
                (tag_name, node_name, tag_id, node_id, data_type,
                 scale_factor, offset, unit_from, unit_to, range_min, range_max,
                 source_type, source_path) = row

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

                if source_type == "neuron" and source_path:
                    parts = source_path.split("/")
                    if len(parts) >= 3:
                        neuron_node, neuron_group = parts[0], parts[1]
                        neuron_tag_name = "/".join(parts[2:])
                        new_neuron_tag_map[(neuron_node, neuron_group, neuron_tag_name)] = (
                            node_id, tag_id, rule
                        )
                        # 归一化规则同时按 Neuron tag 名索引，保证 normalizer 能找到
                        new_rules[neuron_tag_name] = rule

            # 原子替换
            self._rules = new_rules
            self._node_id_map = new_node_id_map
            self._tag_id_map = new_tag_id_map
            self._neuron_tag_map = new_neuron_tag_map

            # 加载告警分级配置（alarm_level + fault_map entries）
            def _fetch_alarm_meta() -> dict[str, dict]:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT t.id AS tag_id, t.name AS tag_name, t.alarm_level,
                                   t.alarm_type, t.alarm_threshold, t.fault_map_id, fm.entries,
                                   n.node_type
                            FROM t_tags t
                            JOIN t_nodes n ON t.node_id = n.id
                            LEFT JOIN t_fault_maps fm ON fm.id = t.fault_map_id
                            WHERE t.alarm_level IN ('error1', 'error2', 'error3')
                              AND t.enabled = TRUE
                        """)
                        meta: dict[str, dict] = {}
                        for row in cur.fetchall():
                            tag_id, tag_name, alarm_level, alarm_type, alarm_threshold, fault_map_id, entries, node_type = row
                            meta[str(tag_id)] = {
                                "tag_name": tag_name,
                                "alarm_level": alarm_level,
                                "fault_map_id": fault_map_id,
                                "fault_map_entries": entries or [],
                            }
                        return meta

            self._alarm_tag_map = _fetch_alarm_meta()
            self._alarm_name_map = {m["tag_name"]: m for m in self._alarm_tag_map.values()}
            # 加载实体-告警等级绑定索引（tag_id -> [binding...]）
            def _fetch_entity_alarm_index() -> dict[str, list[dict]]:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT
                                b.id AS binding_id,
                                b.entity_id,
                                e.name AS entity_name,
                                e.display_name AS entity_display_name,
                                b.alarm_level_id,
                                l.code AS alarm_level_code,
                                l.name AS alarm_level_name,
                                l.severity AS alarm_level_severity,
                                COALESCE(b.trigger_rules, l.trigger_rules) AS trigger_rules,
                                t.id AS tag_id,
                                t.node_id,
                                n.name AS node_name,
                                COALESCE(fm_b.entries, fm_t.entries) AS fault_map_entries
                            FROM t_entity_alarm_bindings b
                            JOIN t_entities e ON e.id = b.entity_id
                            JOIN t_alarm_levels l ON l.id = b.alarm_level_id
                            JOIN t_entity_bindings eb ON eb.entity_id = e.id
                            JOIN t_tags t ON t.id = eb.tag_id
                            JOIN t_nodes n ON n.id = t.node_id
                            LEFT JOIN t_fault_maps fm_b ON fm_b.id = b.fault_map_id
                            LEFT JOIN t_fault_maps fm_t ON fm_t.id = t.fault_map_id
                            WHERE b.enabled = TRUE
                              AND l.enabled = TRUE
                              AND e.enabled = TRUE
                              AND t.enabled = TRUE
                              AND n.enabled = TRUE
                              AND eb.enabled = TRUE
                        """)
                        index: dict[str, list[dict]] = {}
                        columns = [desc[0] for desc in cur.description]
                        for row in cur.fetchall():
                            rec = dict(zip(columns, row))
                            tag_id = str(rec["tag_id"])
                            index.setdefault(tag_id, []).append({
                                "binding_id": str(rec["binding_id"]),
                                "entity_id": str(rec["entity_id"]),
                                "entity_name": rec["entity_name"],
                                "entity_display_name": rec["entity_display_name"],
                                "alarm_level_id": str(rec["alarm_level_id"]),
                                "alarm_level_code": rec["alarm_level_code"],
                                "alarm_level_name": rec["alarm_level_name"],
                                "alarm_level_severity": rec["alarm_level_severity"],
                                "trigger_rules": rec["trigger_rules"] or [],
                                "node_id": str(rec["node_id"]),
                                "node_name": rec["node_name"],
                                "fault_map_entries": rec["fault_map_entries"] or [],
                            })
                        return index

            self._entity_alarm_index = _fetch_entity_alarm_index()


            logger.info(
                "[Pipeline] Loaded {} tag rules, {} neuron source-path mappings, {} alarm tags, {} entity alarm bindings",
                len(self._rules), len(self._neuron_tag_map), len(self._alarm_tag_map), sum(len(v) for v in self._entity_alarm_index.values())
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
        """执行实际写入 (t_telemetry)。"""
        if not self._buffer:
            return
        async with self._buffer_lock:
            batch = self._buffer[:]
            self._buffer.clear()
        try:
            if batch:
                count = await batch_insert_telemetry(batch)
                self.metrics.points_written_db += count
                await upsert_telemetry_latest(batch)
        except Exception as e:
            self.metrics.db_write_errors += 1
            logger.error("[Pipeline] DB write error ({} records): {}",
                        len(batch), e)

    @property
    def uptime_seconds(self) -> float:
        if self._started_at:
            return (datetime.now(timezone.utc) - self._started_at).total_seconds()
        return 0.0
