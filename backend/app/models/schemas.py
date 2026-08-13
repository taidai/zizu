"""
ZiZu F0 数据管道 — Pydantic 数据模型

定义消息在管道中流转的数据结构：
  RawMessage → ParsedMessage → NormalizedPoint → TelemetryRecord

每个阶段对应一个 Hook 的输入/输出类型，确保类型安全。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    """Timezone-aware UTC now (utcnow() 在 Python 3.12+ 已弃用)。"""
    return datetime.now(timezone.utc)


# ══════════════════════════════════════
# 枚举类型
# ══════════════════════════════════════


class TagType(str, Enum):
    PHYSICAL = "PHYSICAL"
    LOGICAL = "LOGICAL"


class DataType(str, Enum):
    FLOAT = "FLOAT"
    INT = "INT"
    BOOL = "BOOL"
    STRING = "STRING"
    ENUM = "ENUM"


class Quality(str, Enum):
    GOOD = 192
    UNCERTAIN = 64
    BAD = 0


class PipelineStatus(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


# ══════════════════════════════════════
# 阶段 0: MQTT 原始消息 (M2 输入)
# ══════════════════════════════════════


class RawMessage(BaseModel):
    """
    从 nanoMQ 收到的原始 MQTT 消息。

    paho-mqtt 回调直接产出此对象。
    """

    topic: str  # e.g. "telemetry/HuaweiInverter_01"
    payload: bytes
    qos: int = 1
    timestamp_recv: datetime = Field(default_factory=_utc_now)


# ══════════════════════════════════════
# 阶段 1: 解析后 (Hook 1 / M2 输出 → M3 输入)
# ══════════════════════════════════════


class ParsedMessage(BaseModel):
    """
    Neuron JSON 解析后的结构化消息。

    Neuron 上报格式示例:
    {
      "node_name": "HuaweiInverter_01",
      "timestamp": 1721223400000,   // ms epoch
      "tags": {
        "activePower": 45200,
        "dcVoltage": 720.5,
        "running": true,
        "status": "normal"
      }
    }
    """

    node_name: str  # Neuron 节点名 (对应 t_nodes.name 或 source_path)
    group: str | None = None  # Neuron 采集组名 (MQTT payload 中常见)
    timestamp_ms: int  # Neuron 上报时间戳 (毫秒 epoch)
    # 原始键值对；允许 list/dict，由 normalizer 决定是否跳过
    tags: dict[str, Any]

    @property
    def timestamp(self) -> datetime:
        """毫秒 epoch → timezone-aware datetime (UTC)。"""
        return datetime.fromtimestamp(self.timestamp_ms / 1000, tz=timezone.utc)

    @property
    def tag_count(self) -> int:
        return len(self.tags)


# ══════════════════════════════════════
# 阶段 2: 归一化后 (Hook 2 / M3 输出 → M4 + M5 输入)
# ══════════════════════════════════════


class NormalizedPoint(BaseModel):
    """
    归一化后的单个数据点。

    经过 scale*value+offset 和 pint 单位转换后的工程值。
    这是管道中间产物，也是 M5 VirtualPointEngine 的输入。
    """

    node_name: str  # 来源节点名 (解析时保留, 后续匹配 node_id)
    group: str | None = None  # 来源采集组名 (Neuron MQTT payload)
    tag_name: str  # 归一化后的字段名 (可能被 rename, 如 activePower→activePower_kW)
    value: float | int | bool | str  # 工程值
    data_type: DataType
    quality: Quality = Quality.GOOD
    ts: datetime  # 数据产生时间 (来自 Neuron timestamp)
    unit: str | None = None  # 转换后的单位
    is_virtual: bool = False  # CE Path A 标记 (F0 阶段恒为 False)


class NormalizedMessage(BaseModel):
    """
    一条 ParsedMessage 归一化后的全部点位集合。

    包含元信息 + NormalizedPoint[] 列表。
    """

    source_node: str  # 原始 node_name
    ts: datetime  # 消息的时间戳
    points: list[NormalizedPoint] = []

    @property
    def point_count(self) -> int:
        return len(self.points)


# ══════════════════════════════════════
# 阶段 3: 入库记录 (Hook 3 / M4 输出 → DB)
# ══════════════════════════════════════


class TelemetryRecord(BaseModel):
    """
    写入 t_telemetry Hypertable 的行记录。

    这是最终的持久化格式，包含 node_id / tag_id 外键。
    """

    ts: datetime
    node_id: UUID
    tag_id: UUID
    value_float: float | None = None
    value_int: int | None = None
    value_bool: bool | None = None
    value_str: str | None = None
    is_virtual: bool = False
    quality: int = 192  # OPC UA quality code

    @classmethod
    def from_point(
        cls,
        point: NormalizedPoint,
        node_id: UUID,
        tag_id: UUID,
    ) -> TelemetryRecord:
        """
        从 NormalizedPoint 创建入库记录 (按**实际值类型**分派)。

        原则: 存储保真优先 — float 值永远进 value_float (保留精度)，
              int 值进 value_int。data_type 仅作元数据标签 (UI 格式化用)，
              不决定存储列，避免 Neuron decimal 缩放后的 float 被 int() 截断。
        """
        common = dict(
            ts=point.ts,
            node_id=node_id,
            tag_id=tag_id,
            is_virtual=point.is_virtual,
            quality=point.quality.value,
        )
        # 注意: bool 是 int 子类, 必须先判 bool
        if isinstance(point.value, bool):
            return cls(**common, value_bool=point.value)
        elif isinstance(point.value, int):
            return cls(**common, value_int=point.value)
        elif isinstance(point.value, float):
            return cls(**common, value_float=point.value)
        else:
            return cls(**common, value_str=str(point.value))


# ══════════════════════════════════════
# 管道运行状态
# ══════════════════════════════════════


class PipelineMetrics(BaseModel):
    """F0 管道运行指标 — 用于 Health API 和监控。"""

    status: PipelineStatus = PipelineStatus.STOPPED
    messages_received: int = 0
    messages_parsed_ok: int = 0
    messages_parse_error: int = 0
    points_normalized: int = 0
    points_written_db: int = 0
    db_write_errors: int = 0
    last_message_at: datetime | None = None
    started_at: datetime | None = None
    uptime_seconds: float = 0.0


# ══════════════════════════════════════
# F3: 节点树域 — 5 层统一模型 (M1 节点树引擎)
# ══════════════════════════════════════
#
#   Site(L1) → Station(L2) → EnergyNode(L3) → Device(L4) → Tag(L5)
#
# 节点树是 F3 的核心：每层可挂载点位 (PhysicalTag / LogicalTag)，
# 父节点通过 LogicalTag 汇总子节点点位 (SUM/AVG/MAX/MIN)。


class NodeLayer(int, Enum):
    """节点层级 (对应 t_nodes.layer CHECK 1~5)。"""

    SITE = 1        # 场站/园区
    STATION = 2     # 站点
    ENERGY_NODE = 3  # 能量节点 (ESS/PV/GRID/EVSE)
    DEVICE = 4      # 设备
    TAG = 5         # 点位挂载层 (通常 tag 直接挂 Device, 该层备用)


class NodeCreate(BaseModel):
    """创建节点请求 (POST /nodes)。"""

    name: str = Field(..., min_length=1, max_length=128, description="节点名")
    parent_id: UUID | None = Field(None, description="父节点 ID (根节点为 None)")
    layer: int = Field(..., ge=1, le=5, description="层级 1~5")
    node_type: str | None = Field(None, description="节点子类型, 如 ESS/PV/GRID/EVSE")
    source_catalog_key: str | None = Field(
        None,
        min_length=1,
        max_length=128,
        description="解决方案匹配使用的站点内稳定设备键",
    )
    config: dict = Field(default_factory=dict, description="扩展配置 JSONB")
    sort_order: int = Field(0, description="同级排序")
    enabled: bool = True


class NodeUpdate(BaseModel):
    """更新节点请求 (PUT /nodes/{id}) — 全部字段可选, 部分更新。"""

    name: str | None = Field(None, min_length=1, max_length=128)
    parent_id: UUID | None = None
    node_type: str | None = None
    config: dict | None = None
    sort_order: int | None = None
    enabled: bool | None = None


class NodeResponse(BaseModel):
    """节点响应 (扁平)。"""

    id: UUID
    name: str
    parent_id: UUID | None = None
    layer: int
    node_type: str | None = None
    config: dict = Field(default_factory=dict)
    sort_order: int = 0
    enabled: bool = True
    tag_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NodeTree(BaseModel):
    """递归树节点 (GET /nodes/{id}/tree)。"""

    id: UUID
    name: str
    parent_id: UUID | None = None
    layer: int
    node_type: str | None = None
    sort_order: int = 0
    enabled: bool = True
    tag_count: int = 0
    children: list["NodeTree"] = Field(default_factory=list)
