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
from app.services.data_trunk import DataTrunk, RawObservationAdapter, TagMetadata
from app.services.data_trunk_contracts import (
    DataTrunkError,
)
from app.services.data_trunk_postgres import build_postgres_data_trunk


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
        # Neuron 点位按 source_path 精确映射: {(neuron_node, group, tag_name): (node_id, tag_id, rule)}
        self._neuron_tag_map: dict[tuple[str, str, str], tuple[UUID, UUID, TagNormalizationRule]] = {}
        self._raw_neuron_tag_map: dict[tuple[str, str, str], TagMetadata] = {}
        self._raw_node_tag_map: dict[tuple[str, str], TagMetadata] = {}

        # ---- Metrics ----
        self.metrics = PipelineMetrics()
        self._started_at: datetime | None = None

        # ---- 数据帧运行时 ----
        self._data_trunk = data_trunk
        self._raw_observation_adapter = RawObservationAdapter()

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

        if self._data_trunk is None:
            self._data_trunk = await asyncio.to_thread(build_postgres_data_trunk)

        # Step 2: 加载归一化规则和 ID 映射
        await self._load_tag_rules()

        # Step 3: 启动 MQTT 客户端
        self._mqtt = MqttClient(on_message_callback=self.on_message)
        await self._mqtt.start()

        # Step 4: 启动 tag 规则动态重载任务
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

        # 断开 MQTT
        if self._mqtt:
            await self._mqtt.stop()

        if self._data_trunk is not None:
            await asyncio.to_thread(self._data_trunk.close)

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

        # Legacy alarm topics are not configuration targets after the L2 hard cut.
        if self._mqtt is not None and self._mqtt.is_alarm_topic(mqtt_msg.topic):
            logger.debug("[Pipeline] Ignored legacy alarm topic after L2 hard cut")
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

        # ── Hook 2: 归一化 (CPU 密集型，放到线程池避免阻塞事件循环) ──
        # 复制 rules 引用避免 reload 期间的竞态；dict 引用替换是原子的。
        rules_snapshot = self._rules
        normalized = await asyncio.to_thread(normalize, parsed, rules=rules_snapshot)
        self.metrics.points_normalized += normalized.point_count

        # 填充 node_id (延迟解析时保留的 node_name 需要映射回 node_id)
        for point in normalized.points:
            point.node_name = parsed.node_name

        # ── Hook 3: 只进入内存黑板；MQTT 热路径不访问数据库 ──
        try:
            receipt = self._data_trunk.accept(raw_observations)
            self.metrics.points_written_db += receipt.accepted_count
        except DataTrunkError as error:
            self.metrics.db_write_errors += 1
            logger.warning("[Pipeline] L0 observation rejected: {}", error.code)

    # ══════════════════════════════
    # 辅助方法
    # ══════════════════════════════

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
                                   t.wire_data_type,
                                   n.source_catalog_key,
                                   t.timestamp_trusted,
                                   t.source_sequence_trusted
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
                 source_type, source_path, wire_data_type, node_source_catalog_key,
                 timestamp_trusted, source_sequence_trusted) = row

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
                    wire_data_type=wire_data_type,
                    source_sequence_trusted=bool(source_sequence_trusted),
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

            logger.info(
                "[Pipeline] Loaded {} tag rules and {} Neuron source-path mappings",
                len(self._rules), len(self._neuron_tag_map),
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

    async def _periodic_reload_rules(self) -> None:
        """定时重载 tag 规则，让新导入点位无需重启即可生效。"""
        while True:
            await asyncio.sleep(settings.pipeline_reload_rules_interval_sec)
            try:
                await self._load_tag_rules()
            except Exception as e:
                logger.warning("[Pipeline] Periodic reload rules failed: {}", e)

    @property
    def data_trunk(self) -> DataTrunk:
        if self._data_trunk is None:
            raise RuntimeError("data-frame runtime has not started")
        return self._data_trunk

    @property
    def uptime_seconds(self) -> float:
        if self._started_at:
            return (datetime.now(timezone.utc) - self._started_at).total_seconds()
        return 0.0
