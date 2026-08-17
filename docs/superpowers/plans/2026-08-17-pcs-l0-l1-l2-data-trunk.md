# PCS L0—L1—L2 数据主干 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以一台 PCS 和两套品牌模板交付首条可信数据主干，使协议原始点位通过版本化点位转换形成稳定 L2 全局实体，并由公开 PostgreSQL、REST、WebSocket 与机器报告证明原子性、质量、换牌和权限语义。

**Architecture:** 保留 parser 只负责协议解码，把 `DataTrunk.ingest(raw_observations)` 建成唯一数据写入深模块；其 PostgreSQL adapter 在一个事务内写 L0 history/latest、固定 L1 revision 的 L2 history/latest、来源关系和 outbox，提交后才确认 pipeline buffer。解决方案包携带不可变点位转换模板，首次安装在同一站点配置事务中创建稳定实体身份和转换绑定，节点独立计划用于换牌且不得改变 L2 实体 ID。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、psycopg2、PostgreSQL/TimescaleDB、React 18、TypeScript、Vite、原生 WebSocket；不新增运行时依赖。

## Global Constraints

- 第一纵向切片只包含概念语义 `pcs.activePower`、`pcs.operatingState`、`pcs.faultCodes`；机器 ID 沿用仓库既有 snake_case 规则 `pcs.active_power`、`pcs.operating_state`、`pcs.fault_codes`，不扩展任意表达式、跨节点公式或反向控制换算。
- 物理节点树只表达物理归属；L0、L1、L2 是选中节点后的数据主干视图，不创建伪物理子节点。
- L0 使用 `t_telemetry`/`t_telemetry_latest` 的新增 raw 列保存品牌事实；既有 `value_*` 只作为 expand 阶段兼容投影，L1 必须读取 raw 列。
- L2 质量固定为 `GOOD=192`、`UNCERTAIN=64`、`BAD=0`、`STALE=1`；BAD/STALE 的所有强类型值必须为空。
- L0、L2、latest、source relations、outbox 必须同事务生死与共；迟到观测可进 history，但不得倒退 latest 或产生实时推送。
- 上层告警、策略、控制、画面、历史和报表只引用 L2 实体实例；已迁移 PCS 缺少 point conversion 时显式失败，禁止回退 direct Tag。
- 首次解决方案安装必须原子创建实体身份和点位转换绑定；节点换牌只改变模板 revision/输入绑定，不改变 L2 实体实例 ID。
- 所有必需输入 100% 解析才可应用；零候选、多候选、类型不符、单位不符均形成结构化 blocker，零运行写入。
- HTTP/WS 复用既有身份、能力和审计；仅 `/health/live` 匿名，WS token 不放 query string且订阅期间持续重验 session。
- 实施界面不显示 UUID、SQL、JSON/YAML、寄存器地址或凭据；参考资产不包含客户参数和真实现场拓扑。
- 每个任务先得到公共或领域 seam 的 RED，再写最少实现；每个任务结束运行定向测试并独立提交。
- 后端最终门禁为完整 unittest、真实 PostgreSQL 主缝、`compileall`；前端最终门禁为 Node 契约测试、`npx tsc -b`、`npm run build`。

---

## File Map

### 新建的聚焦模块

| 文件 | 单一职责 |
|---|---|
| `backend/app/services/data_trunk_contracts.py` | L0/L2 强类型值、质量、来源、receipt 和稳定机器错误 |
| `backend/app/services/data_trunk_conversion.py` | 纯函数转换内核：数值、枚举、多码、质量与确定性 event ID |
| `backend/app/services/data_trunk.py` | 唯一公开 `DataTrunk.ingest()` 深模块及内部 freshness 调度入口 |
| `backend/app/services/data_trunk_postgres.py` | 单连接事务写入 L0/L2/latest/source/outbox/failure |
| `backend/app/services/point_conversion.py` | 点位转换模板目录、确定性 plan/apply 领域行为 |
| `backend/app/services/point_conversion_postgres.py` | L1 关系、计划、安装和换牌的 PostgreSQL adapter |
| `backend/app/services/solution_point_conversions.py` | 解决方案包资产解析及首次安装参与者 |
| `backend/app/services/data_trunk_outbox.py` | 只分发已提交的 L2 outbox，并按 event ID 确认 |
| `backend/app/services/data_trunk_acceptance.py` | 生成既有 delivery report 的 `data_trunk` 验收项 |
| `backend/app/api/point_conversions.py` | 模板、节点数据主干、plan/apply REST seam |
| `init-db/migration_038_pcs_data_trunk.sql` | L0 raw、L1 关系、L2 时序、outbox 和失败队列的 expand migration |
| `init-db/migration_039_pcs_data_trunk_contract_gate.sql` | migrated PCS 禁止 direct-tag 双来源和运行 fallback 的数据库门禁 |
| `frontend/src/components/data-trunk/DataTrunkWorkspace.tsx` | 五阶段引导式交付驾驶舱容器 |
| `frontend/src/components/data-trunk/NodeTrunkOverview.tsx` | L0→L1→L2 可读主视图 |
| `frontend/src/components/data-trunk/PointConversionPlanPanel.tsx` | 计划差异、blocker、幂等应用和换牌摘要 |
| `frontend/src/components/data-trunk/EntityObservationCard.tsx` | L2 值、单位、质量、年龄、revision、来源摘要 |
| `frontend/src/components/data-trunk/dataTrunkRetryState.ts` | actor/node/plan 绑定的 session 重试上下文 |

### 修改的现有模块

| 文件 | 责任变化 |
|---|---|
| `backend/app/services/pipeline.py` | parser 后组装 raw observations；receipt 后才删除 buffer |
| `backend/app/services/telemetry_store.py` | 旧独立双提交函数降为查询/兼容 helper，禁止 pipeline 调用双写路径 |
| `backend/app/services/solution_package_archive.py` | 校验 `point_conversion_template` 资产和 `data_trunk` acceptance |
| `backend/app/services/solution_delivery_contracts.py` | installation plan 持久化 point-conversion 子计划 |
| `backend/app/services/solution_delivery.py` | 首次安装同时规划实体身份和点位转换，验收复用既有报告 |
| `backend/app/services/solution_delivery_repository.py` | 在既有站点配置事务内调用 point-conversion installation participant |
| `backend/app/api/solution_delivery.py` | `CreateInstallationPlanRequest.point_conversions` 强类型请求 |
| `backend/app/services/entity_instance_registry.py` | 支持 `sourceKind: point_conversion` 的稳定实体身份，不创建 direct-tag binding |
| `backend/app/services/entity_instance_runtime.py` | 按来源种类读取 L2 或 legacy Tag；point-conversion 失败不回退 |
| `backend/app/services/entity_instance_postgres.py` | 加载 device node、source kind 和 L2 catalog |
| `backend/app/api/entity_instances.py` | 增加 L2 history 路由并保持 realtime 契约 |
| `backend/app/api/websocket.py` | 增加认证 `/ws/entity-observations`，旧 telemetry WS 保留给未迁移范围 |
| `backend/app/api/business_security.py` | 登记新 REST 路由所需既有 capability，不新增角色枚举 |
| `backend/app/main.py` | 注册 point conversion router、outbox/freshness 生命周期 |
| `backend/tests/postgres_delivery_app.py` | 协议模拟器走真实 parser→DataTrunk，并挂载新路由/WS |
| `frontend/src/api/client.ts` | 新 typed REST/WS client，禁止 L2 页面读取旧 telemetry WS |
| `frontend/src/pages/NodeTreePage.tsx` | 选中 PCS 后显示数据主干驾驶舱，operator 保持 L2 只读 |
| `reference-deliveries/pv-storage-charging-ems/package.yaml` | 声明两套 PCS 模板、三项实体和 data-trunk acceptance |
| `reference-deliveries/pv-storage-charging-ems/slots/pcs.yaml` | PCS 读实体改为 `sourceKind: point_conversion` |
| `README.md`、`CONTEXT.md`、`docs/product-destination.md` | 执行完成后同步公开格式、边界和完成事实；保留当前未提交内容 |
| `CODEX_HANDOFF.md` | 每个执行会话记录提交、测试、阻断和现场边界 |

---

### Task 1: 定义数据主干契约并完成单条 PCS 数值转换

**Files:**
- Create: `backend/app/services/data_trunk_contracts.py`
- Create: `backend/app/services/data_trunk_conversion.py`
- Create: `backend/tests/test_data_trunk_conversion.py`

**Interfaces:**
- Consumes: Python 标准库 `dataclasses`、`datetime`、`enum`、`hashlib`、`uuid`。
- Produces: `TrunkQuality`、`ValueKind`、`TypedValue`、`RawObservation`、`InputReference`、`NumericTransform`、`InstalledPointConversion`、`L2Observation`、`CommitReceipt`、`DataTrunkError`；`evaluate_conversion(*, installed, current_inputs, site_configuration_version, calculated_at) -> tuple[L2Observation, ...]`。

- [ ] **Step 1: 写数值转换 RED**

```python
class PcsNumericConversionTest(unittest.TestCase):
    def test_scales_raw_watts_to_stable_kw_entity(self):
        raw = RawObservation(
            observation_id=UUID("00000000-0000-0000-0000-000000000101"),
            node_id=UUID("00000000-0000-0000-0000-000000000001"),
            tag_id=UUID("00000000-0000-0000-0000-000000000011"),
            source_key="ActivePowerRaw",
            value=TypedValue.float(12345.0),
            raw_unit="W",
            quality=TrunkQuality.GOOD,
            source_timestamp=datetime(2026, 8, 17, tzinfo=UTC),
            received_at=datetime(2026, 8, 17, 0, 0, 1, tzinfo=UTC),
            source_message_id="msg-1",
            source_sequence=1,
            source_digest="a" * 64,
        )
        installed = InstalledPointConversion.numeric(
            installation_id=UUID("00000000-0000-0000-0000-000000000201"),
            revision_id=UUID("00000000-0000-0000-0000-000000000202"),
            input_tag_id=raw.tag_id,
            output_entity_instance_id=UUID("00000000-0000-0000-0000-000000000301"),
            output_definition_id="pcs.active_power",
            scale=0.001,
            offset=0.0,
            input_unit="W",
            output_unit="kW",
            minimum=-500.0,
            maximum=500.0,
        )

        result = evaluate_conversion(
            installed=(installed,),
            current_inputs={InputReference.l0(raw.tag_id): raw},
            site_configuration_version=4,
            calculated_at=datetime(2026, 8, 17, 0, 0, 2, tzinfo=UTC),
        )

        self.assertEqual(result[0].value, TypedValue.float(12.345))
        self.assertEqual(result[0].unit, "kW")
        self.assertEqual(result[0].quality, TrunkQuality.GOOD)
        self.assertEqual(result[0].source_observation_ids, (raw.observation_id,))
        self.assertEqual(result[0].site_configuration_version, 4)
```

- [ ] **Step 2: 运行测试并确认正确失败**

Run: `cd backend; python -m unittest tests.test_data_trunk_conversion.PcsNumericConversionTest.test_scales_raw_watts_to_stable_kw_entity -v`

Expected: `ModuleNotFoundError: No module named 'app.services.data_trunk_contracts'`。

- [ ] **Step 3: 实现不可变契约和数值内核**

```python
class TrunkQuality(IntEnum):
    BAD = 0
    STALE = 1
    UNCERTAIN = 64
    GOOD = 192


class ValueKind(str, Enum):
    FLOAT = "FLOAT"
    INT = "INT"
    BOOL = "BOOL"
    STRING = "STRING"
    ENUM = "ENUM"
    CODE_SET = "CODE_SET"


@dataclass(frozen=True)
class TypedValue:
    kind: ValueKind
    value: float | int | bool | str | tuple[str, ...] | None

    @classmethod
    def float(cls, value: float | None) -> "TypedValue":
        return cls(ValueKind.FLOAT, value)


@dataclass(frozen=True)
class RawObservation:
    observation_id: UUID
    node_id: UUID
    tag_id: UUID
    source_key: str
    value: TypedValue
    raw_unit: str | None
    quality: TrunkQuality
    source_timestamp: datetime
    received_at: datetime
    source_message_id: str | None
    source_sequence: int | None
    source_digest: str


@dataclass(frozen=True)
class InputReference:
    source_kind: str
    source_id: UUID

    @classmethod
    def l0(cls, tag_id: UUID) -> "InputReference":
        return cls("l0", tag_id)

    @classmethod
    def l2(cls, entity_instance_id: UUID) -> "InputReference":
        return cls("l2", entity_instance_id)


@dataclass(frozen=True)
class NumericTransform:
    input: InputReference
    scale: float
    offset: float
    input_unit: str | None
    minimum: float | None
    maximum: float | None


@dataclass(frozen=True)
class InstalledPointConversion:
    installation_id: UUID
    revision_id: UUID
    entity_instance_id: UUID
    entity_definition_id: str
    output_kind: ValueKind
    output_unit: str | None
    freshness_seconds: float
    transform: NumericTransform

    @classmethod
    def numeric(
        cls,
        *,
        installation_id: UUID,
        revision_id: UUID,
        input_tag_id: UUID,
        output_entity_instance_id: UUID,
        output_definition_id: str,
        scale: float,
        offset: float,
        input_unit: str | None,
        output_unit: str | None,
        minimum: float | None,
        maximum: float | None,
    ) -> "InstalledPointConversion":
        return cls(
            installation_id,
            revision_id,
            output_entity_instance_id,
            output_definition_id,
            ValueKind.FLOAT,
            output_unit,
            30.0,
            NumericTransform(InputReference.l0(input_tag_id), scale, offset, input_unit, minimum, maximum),
        )


@dataclass(frozen=True)
class L2Observation:
    event_id: UUID
    entity_instance_id: UUID
    definition_id: str
    value: TypedValue
    unit: str | None
    quality: TrunkQuality
    reason: str | None
    observed_at: datetime
    received_at: datetime
    calculated_at: datetime
    conversion_revision_id: UUID
    site_configuration_version: int
    source_observation_ids: tuple[UUID, ...]
    source_digest: str
    source_order_key: str


@dataclass(frozen=True)
class CommitReceipt:
    transaction_id: UUID
    accepted_l0_count: int
    duplicate_l0_count: int
    l2_event_ids: tuple[UUID, ...]
    late_observation_count: int
    failure_reference: UUID | None = None


class DataTrunkError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def evaluate_conversion(
    *,
    installed: tuple[InstalledPointConversion, ...],
    current_inputs: Mapping[InputReference, RawObservation | L2Observation],
    site_configuration_version: int,
    calculated_at: datetime,
) -> tuple[L2Observation, ...]:
    outputs = tuple(
        _evaluate_output(item, current_inputs, site_configuration_version, calculated_at)
        for item in installed
    )
    return tuple(sorted(outputs, key=lambda item: str(item.entity_instance_id)))
```

`_evaluate_output()` 对 numeric rule 精确执行 `(raw * scale) + offset`，先验证输入类型/单位，再验证输出范围；运行观测的类型、单位或范围错误生成 BAD/null L2，不抛异常回滚已经合法的 L0；只有配置损坏或 repository 不可用抛 `DataTrunkError`。`event_id` 使用 `uuid5(NAMESPACE_URL, revision_id + entity_id + sorted(source observation ids) + quality + canonical value)`，保证重放稳定。

- [ ] **Step 4: 增加不可变、单位错配和 event ID 确定性用例**

```python
def test_numeric_conversion_marks_wrong_runtime_unit_bad_without_value(self):
    fixture = self.fixture()
    raw = dataclasses.replace(fixture["raw"], raw_unit="A")
    output = evaluate_conversion(
        installed=fixture["installed"],
        current_inputs={InputReference.l0(raw.tag_id): raw},
        site_configuration_version=fixture["site_configuration_version"],
        calculated_at=fixture["calculated_at"],
    )[0]
    self.assertEqual(output.value, TypedValue.float(None))
    self.assertEqual((output.quality, output.reason), (TrunkQuality.BAD, "UNIT_MISMATCH"))

def test_same_inputs_produce_same_event_id(self):
    first = evaluate_conversion(**self.fixture())
    second = evaluate_conversion(**self.fixture())
    self.assertEqual(first[0].event_id, second[0].event_id)
```

- [ ] **Step 5: 运行 Task 1 全部测试**

Run: `cd backend; python -m unittest tests.test_data_trunk_conversion -v`

Expected: 全部 PASS；测试不连接数据库。

- [ ] **Step 6: 提交 Task 1**

```bash
git add backend/app/services/data_trunk_contracts.py backend/app/services/data_trunk_conversion.py backend/tests/test_data_trunk_conversion.py
git commit -m "feat(data): add PCS numeric conversion kernel"
```

---

### Task 2: 建立 Migration 038 和单条数值转换的原子 PostgreSQL 主缝

**Files:**
- Create: `init-db/migration_038_pcs_data_trunk.sql`
- Create: `backend/app/services/data_trunk.py`
- Create: `backend/app/services/data_trunk_postgres.py`
- Create: `backend/tests/test_data_trunk_migration_postgres.py`
- Create: `backend/tests/test_data_trunk_postgres.py`

**Interfaces:**
- Consumes: Task 1 的 `RawObservation`、`L2Observation`、`CommitReceipt` 和 `evaluate_conversion()`；现有 `telemetry_store.get_connection()`。
- Produces: `DataTrunk.ingest(raw_observations: Sequence[RawObservation]) -> CommitReceipt`；`PostgresDataTrunkRepository.transact(raw_observations, evaluator) -> CommitReceipt`。

- [ ] **Step 1: 写 migration fresh/upgrade/replay RED**

```python
class DataTrunkMigrationPostgresTest(PostgresIsolatedTestCase):
    def test_038_upgrades_037_and_replays(self):
        self.run_migrations_through("migration_037_alarm_configuration_application_kinds.sql")
        self.run_sql("init-db/migration_038_pcs_data_trunk.sql")
        self.run_sql("init-db/migration_038_pcs_data_trunk.sql")
        self.assert_table("t_point_conversion_revisions")
        self.assert_table("t_l2_observations")
        self.assert_table("t_l2_latest")
        self.assert_table("t_l2_stream_outbox")
        self.assert_column("t_telemetry", "raw_value_float")
        self.assert_column("t_telemetry_latest", "source_digest")
```

- [ ] **Step 2: 运行 migration RED**

Run: `$env:ZIZU_POSTGRES_TEST='1'; cd backend; python -m unittest tests.test_data_trunk_migration_postgres -v`

Expected: FAIL，原因是 `migration_038_pcs_data_trunk.sql` 不存在。

- [ ] **Step 3: 创建精确关系结构**

Migration 038 使用 `BEGIN`/`COMMIT`，并创建下列约束（名称也固定，供测试和启动门禁查询）：

```sql
ALTER TABLE t_telemetry
  ADD COLUMN IF NOT EXISTS observation_id UUID,
  ADD COLUMN IF NOT EXISTS source_message_id TEXT,
  ADD COLUMN IF NOT EXISTS source_sequence BIGINT,
  ADD COLUMN IF NOT EXISTS source_digest CHAR(64),
  ADD COLUMN IF NOT EXISTS raw_unit TEXT,
  ADD COLUMN IF NOT EXISTS raw_value_float DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS raw_value_int BIGINT,
  ADD COLUMN IF NOT EXISTS raw_value_bool BOOLEAN,
  ADD COLUMN IF NOT EXISTS raw_value_text TEXT;

ALTER TABLE t_telemetry_latest
  ADD COLUMN IF NOT EXISTS observation_id UUID,
  ADD COLUMN IF NOT EXISTS source_message_id TEXT,
  ADD COLUMN IF NOT EXISTS source_sequence BIGINT,
  ADD COLUMN IF NOT EXISTS source_digest CHAR(64),
  ADD COLUMN IF NOT EXISTS source_order_key TEXT,
  ADD COLUMN IF NOT EXISTS raw_unit TEXT,
  ADD COLUMN IF NOT EXISTS raw_value_float DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS raw_value_int BIGINT,
  ADD COLUMN IF NOT EXISTS raw_value_bool BOOLEAN,
  ADD COLUMN IF NOT EXISTS raw_value_text TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_telemetry_source_observation
ON t_telemetry(tag_id, ts, source_digest)
WHERE source_digest IS NOT NULL;

CREATE TABLE IF NOT EXISTS t_l0_observation_dedup (
  observation_id UUID PRIMARY KEY,
  tag_id UUID NOT NULL REFERENCES t_tags(id),
  observed_at TIMESTAMPTZ NOT NULL,
  source_digest CHAR(64) NOT NULL UNIQUE,
  source_message_id TEXT,
  source_sequence BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE t_telemetry
  DROP CONSTRAINT IF EXISTS fk_telemetry_l0_observation,
  ADD CONSTRAINT fk_telemetry_l0_observation
    FOREIGN KEY(observation_id) REFERENCES t_l0_observation_dedup(observation_id),
  DROP CONSTRAINT IF EXISTS chk_telemetry_raw_typed_value,
  ADD CONSTRAINT chk_telemetry_raw_typed_value CHECK (
    source_digest IS NULL
    OR (observation_id IS NOT NULL AND num_nonnulls(raw_value_float,raw_value_int,raw_value_bool,raw_value_text)=1)
  );

ALTER TABLE t_telemetry_latest
  DROP CONSTRAINT IF EXISTS fk_telemetry_latest_l0_observation,
  ADD CONSTRAINT fk_telemetry_latest_l0_observation
    FOREIGN KEY(observation_id) REFERENCES t_l0_observation_dedup(observation_id),
  DROP CONSTRAINT IF EXISTS chk_telemetry_latest_raw_typed_value,
  ADD CONSTRAINT chk_telemetry_latest_raw_typed_value CHECK (
    source_digest IS NULL
    OR (observation_id IS NOT NULL AND num_nonnulls(raw_value_float,raw_value_int,raw_value_bool,raw_value_text)=1)
  );

ALTER TABLE t_entity_instances
  ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'legacy_tag',
  DROP CONSTRAINT IF EXISTS t_entity_instances_data_type_check;
ALTER TABLE t_entity_instances
  ADD CONSTRAINT t_entity_instances_data_type_check
  CHECK (data_type IN ('FLOAT','INT','BOOL','STRING','ENUM','CODE_SET')),
  DROP CONSTRAINT IF EXISTS chk_entity_instance_source_kind,
  ADD CONSTRAINT chk_entity_instance_source_kind
  CHECK (source_kind IN ('legacy_tag','point_conversion'));

ALTER TABLE t_device_instances ADD COLUMN IF NOT EXISTS node_id UUID REFERENCES t_nodes(id);

ALTER TABLE t_solution_install_plans
  ADD COLUMN IF NOT EXISTS point_conversion_plans JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS t_point_conversion_templates (
  id UUID PRIMARY KEY,
  asset_id TEXT NOT NULL,
  device_category TEXT NOT NULL,
  brand TEXT NOT NULL,
  model TEXT NOT NULL,
  display_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','retired')),
  UNIQUE(asset_id, brand, model)
);

CREATE TABLE IF NOT EXISTS t_point_conversion_revisions (
  id UUID PRIMARY KEY,
  template_id UUID NOT NULL REFERENCES t_point_conversion_templates(id),
  revision INTEGER NOT NULL CHECK (revision > 0),
  content_digest CHAR(64) NOT NULL,
  published_at TIMESTAMPTZ NOT NULL,
  UNIQUE(template_id, revision),
  UNIQUE(template_id, content_digest)
);

CREATE TABLE IF NOT EXISTS t_solution_point_conversion_assets (
  package_record_id UUID NOT NULL REFERENCES t_solution_packages(id),
  template_revision_id UUID NOT NULL REFERENCES t_point_conversion_revisions(id),
  asset_id TEXT NOT NULL,
  PRIMARY KEY(package_record_id, asset_id),
  UNIQUE(package_record_id, template_revision_id)
);

CREATE TABLE IF NOT EXISTS t_point_conversion_inputs (
  id UUID PRIMARY KEY,
  revision_id UUID NOT NULL REFERENCES t_point_conversion_revisions(id),
  input_key TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('l0','l2')),
  data_type TEXT NOT NULL CHECK (data_type IN ('FLOAT','INT','BOOL','STRING','ENUM','CODE_SET')),
  unit TEXT,
  required BOOLEAN NOT NULL,
  stable_source_key TEXT NOT NULL,
  aliases TEXT[] NOT NULL DEFAULT '{}',
  UNIQUE(revision_id, input_key)
);

CREATE TABLE IF NOT EXISTS t_point_conversion_outputs (
  id UUID PRIMARY KEY,
  revision_id UUID NOT NULL REFERENCES t_point_conversion_revisions(id),
  output_key TEXT NOT NULL,
  entity_definition_id TEXT NOT NULL,
  data_type TEXT NOT NULL CHECK (data_type IN ('FLOAT','INT','BOOL','STRING','ENUM','CODE_SET')),
  unit TEXT,
  freshness_seconds DOUBLE PRECISION NOT NULL CHECK (freshness_seconds > 0),
  UNIQUE(revision_id, output_key)
);

CREATE TABLE IF NOT EXISTS t_numeric_transform_rules (
  output_id UUID PRIMARY KEY REFERENCES t_point_conversion_outputs(id),
  input_id UUID NOT NULL REFERENCES t_point_conversion_inputs(id),
  scale DOUBLE PRECISION NOT NULL CHECK (scale = scale AND abs(scale) < 1e308),
  offset DOUBLE PRECISION NOT NULL CHECK (offset = offset AND abs(offset) < 1e308),
  minimum DOUBLE PRECISION CHECK (minimum = minimum AND abs(minimum) < 1e308),
  maximum DOUBLE PRECISION CHECK (maximum = maximum AND abs(maximum) < 1e308),
  CHECK (minimum IS NULL OR maximum IS NULL OR minimum <= maximum)
);

CREATE TABLE IF NOT EXISTS t_enum_mapping_entries (
  output_id UUID NOT NULL REFERENCES t_point_conversion_outputs(id),
  raw_value TEXT NOT NULL,
  canonical_value TEXT NOT NULL,
  PRIMARY KEY(output_id, raw_value)
);

CREATE TABLE IF NOT EXISTS t_fault_code_mapping_entries (
  output_id UUID NOT NULL REFERENCES t_point_conversion_outputs(id),
  raw_code TEXT NOT NULL,
  canonical_code TEXT NOT NULL,
  display_name TEXT NOT NULL,
  default_severity TEXT NOT NULL CHECK (default_severity IN ('CRITICAL','MAJOR','WARNING','INFO')),
  PRIMARY KEY(output_id, raw_code)
);

CREATE TABLE IF NOT EXISTS t_point_conversion_plans (
  id UUID PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'point_conversion' CHECK (kind = 'point_conversion'),
  node_id UUID NOT NULL REFERENCES t_nodes(id),
  template_revision_id UUID NOT NULL REFERENCES t_point_conversion_revisions(id),
  entity_identity_installation_id UUID NOT NULL,
  solution_installation_id UUID NOT NULL,
  base_site_configuration_version BIGINT NOT NULL,
  source_catalog_digest CHAR(64) NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ready','blocked','applied')),
  items JSONB NOT NULL,
  blockers JSONB NOT NULL,
  digest CHAR(64) NOT NULL,
  planned_by TEXT NOT NULL CHECK (btrim(planned_by) <> ''),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t_point_conversion_plan_items (
  plan_id UUID NOT NULL REFERENCES t_point_conversion_plans(id),
  item_key TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('add','update','preserve','delete_candidate','block')),
  input_id UUID REFERENCES t_point_conversion_inputs(id),
  output_id UUID REFERENCES t_point_conversion_outputs(id),
  source_kind TEXT CHECK (source_kind IN ('l0','l2')),
  selected_tag_id UUID REFERENCES t_tags(id),
  selected_entity_instance_id UUID REFERENCES t_entity_instances(id),
  output_entity_instance_id UUID,
  blocker_code TEXT,
  before_value JSONB,
  after_value JSONB,
  PRIMARY KEY(plan_id, item_key),
  CHECK (
    source_kind IS NULL
    OR (source_kind='l0' AND selected_tag_id IS NOT NULL AND selected_entity_instance_id IS NULL)
    OR (source_kind='l2' AND selected_tag_id IS NULL AND selected_entity_instance_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS t_installed_point_conversions (
  id UUID PRIMARY KEY,
  node_id UUID NOT NULL REFERENCES t_nodes(id),
  revision_id UUID NOT NULL REFERENCES t_point_conversion_revisions(id),
  source_plan_id UUID NOT NULL REFERENCES t_point_conversion_plans(id),
  solution_installation_id UUID NOT NULL
    REFERENCES t_solution_installations(id) DEFERRABLE INITIALLY DEFERRED,
  site_configuration_version BIGINT NOT NULL,
  installed_by TEXT NOT NULL CHECK (btrim(installed_by) <> ''),
  installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  current BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_installed_point_conversion_current
ON t_installed_point_conversions(node_id) WHERE current = TRUE;

CREATE TABLE IF NOT EXISTS t_conversion_input_bindings (
  installed_conversion_id UUID NOT NULL REFERENCES t_installed_point_conversions(id),
  input_id UUID NOT NULL REFERENCES t_point_conversion_inputs(id),
  source_kind TEXT NOT NULL CHECK (source_kind IN ('l0','l2')),
  l0_tag_id UUID REFERENCES t_tags(id),
  l2_entity_instance_id UUID REFERENCES t_entity_instances(id),
  confirmed_by TEXT NOT NULL CHECK (btrim(confirmed_by) <> ''),
  confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(installed_conversion_id, input_id),
  CHECK (
    (source_kind='l0' AND l0_tag_id IS NOT NULL AND l2_entity_instance_id IS NULL)
    OR (source_kind='l2' AND l0_tag_id IS NULL AND l2_entity_instance_id IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS t_conversion_output_bindings (
  installed_conversion_id UUID NOT NULL REFERENCES t_installed_point_conversions(id),
  output_id UUID NOT NULL REFERENCES t_point_conversion_outputs(id),
  entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  PRIMARY KEY(installed_conversion_id, output_id),
  UNIQUE(installed_conversion_id, entity_instance_id)
);

CREATE TABLE IF NOT EXISTS t_point_conversion_applications (
  id UUID PRIMARY KEY,
  plan_id UUID NOT NULL REFERENCES t_point_conversion_plans(id),
  installed_conversion_id UUID NOT NULL REFERENCES t_installed_point_conversions(id),
  solution_installation_id UUID NOT NULL
    REFERENCES t_solution_installations(id) DEFERRABLE INITIALLY DEFERRED,
  site_configuration_version BIGINT NOT NULL,
  actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
  output_entity_instance_ids UUID[] NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t_point_conversion_idempotency (
  actor TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_digest CHAR(64) NOT NULL,
  application_id UUID NOT NULL REFERENCES t_point_conversion_applications(id),
  PRIMARY KEY(actor, idempotency_key)
);
```

`t_point_conversion_revisions`、inputs/outputs/rules/mapping entries、applications 和 idempotency 均用已有 append-only trigger 模式拒绝 UPDATE/DELETE/TRUNCATE；plan 只允许 `ready|blocked → applied`，且 applied 后不可改变 digest/items/blockers。

时序与可靠性表使用以下核心列和 CHECK：

```sql
CREATE TABLE IF NOT EXISTS t_l2_observations (
  observed_at TIMESTAMPTZ NOT NULL,
  event_id UUID NOT NULL,
  entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  received_at TIMESTAMPTZ NOT NULL,
  calculated_at TIMESTAMPTZ NOT NULL,
  value_float DOUBLE PRECISION,
  value_int BIGINT,
  value_bool BOOLEAN,
  value_text TEXT,
  value_codes TEXT[],
  quality SMALLINT NOT NULL CHECK (quality IN (0,1,64,192)),
  reason TEXT,
  conversion_revision_id UUID NOT NULL REFERENCES t_point_conversion_revisions(id),
  site_configuration_version BIGINT NOT NULL,
  source_digest CHAR(64) NOT NULL,
  source_order_key TEXT NOT NULL,
  CONSTRAINT chk_l2_typed_value CHECK (
    (quality IN (0,1) AND num_nonnulls(value_float,value_int,value_bool,value_text,value_codes)=0)
    OR
    (quality IN (64,192) AND num_nonnulls(value_float,value_int,value_bool,value_text,value_codes)=1)
  ),
  UNIQUE(event_id, observed_at)
);
SELECT create_hypertable('t_l2_observations','observed_at',if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS t_l2_latest (
  entity_instance_id UUID PRIMARY KEY REFERENCES t_entity_instances(id),
  event_id UUID NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  calculated_at TIMESTAMPTZ NOT NULL,
  value_float DOUBLE PRECISION,
  value_int BIGINT,
  value_bool BOOLEAN,
  value_text TEXT,
  value_codes TEXT[],
  quality SMALLINT NOT NULL CHECK (quality IN (0,1,64,192)),
  reason TEXT,
  conversion_revision_id UUID NOT NULL REFERENCES t_point_conversion_revisions(id),
  site_configuration_version BIGINT NOT NULL,
  source_digest CHAR(64) NOT NULL,
  source_order_key TEXT NOT NULL,
  CONSTRAINT chk_l2_latest_typed_value CHECK (
    (quality IN (0,1) AND num_nonnulls(value_float,value_int,value_bool,value_text,value_codes)=0)
    OR
    (quality IN (64,192) AND num_nonnulls(value_float,value_int,value_bool,value_text,value_codes)=1)
  )
);

CREATE TABLE IF NOT EXISTS t_l2_observation_sources (
  l2_event_id UUID NOT NULL,
  l2_observed_at TIMESTAMPTZ NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('l0','l2','freshness')),
  l0_observation_id UUID REFERENCES t_l0_observation_dedup(observation_id),
  source_l2_event_id UUID,
  source_l2_observed_at TIMESTAMPTZ,
  source_digest CHAR(64) NOT NULL,
  PRIMARY KEY(l2_event_id, l2_observed_at, source_kind, source_digest),
  FOREIGN KEY(l2_event_id, l2_observed_at)
    REFERENCES t_l2_observations(event_id, observed_at),
  FOREIGN KEY(source_l2_event_id, source_l2_observed_at)
    REFERENCES t_l2_observations(event_id, observed_at),
  CHECK (
    (source_kind='l0' AND l0_observation_id IS NOT NULL AND source_l2_event_id IS NULL AND source_l2_observed_at IS NULL)
    OR
    (source_kind IN ('l2','freshness') AND l0_observation_id IS NULL AND source_l2_event_id IS NOT NULL AND source_l2_observed_at IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS t_l2_stream_outbox (
  event_id UUID PRIMARY KEY,
  entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_by UUID,
  claimed_until TIMESTAMPTZ,
  CHECK ((claimed_by IS NULL) = (claimed_until IS NULL))
);

CREATE TABLE IF NOT EXISTS t_ingestion_failures (
  id UUID PRIMARY KEY,
  source_digest CHAR(64) NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('parse','l0','conversion','l2','outbox')),
  safe_summary JSONB NOT NULL,
  attempts INTEGER NOT NULL CHECK (attempts > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);
```

`t_ingestion_failures.safe_summary` 不得保存 payload/address/token；对应测试向嵌套输入注入这些字段并断言数据库文本零命中。

Migration 038 同时建立 `validate_l2_typed_value_against_entity()` trigger：FLOAT/INT/BOOL/STRING/ENUM/CODE_SET 分别只允许对应列；BAD/STALE 不检查值列种类但仍要求全空。migration 测试逐一插入错列并断言 `check_violation`，保证 API 类型判断之外还有数据库门禁。

- [ ] **Step 4: 运行 migration 测试至 GREEN**

Run: `$env:ZIZU_POSTGRES_TEST='1'; cd backend; python -m unittest tests.test_data_trunk_migration_postgres -v`

Expected: fresh、037 upgrade、replay、typed-value 正负约束全部 PASS。

- [ ] **Step 5: 写事务原子性 RED**

```python
def test_numeric_ingest_commits_l0_l2_latest_source_and_outbox_together(self):
    receipt = self.trunk.ingest((self.raw_power(12345.0, sequence=1),))
    self.assertEqual(receipt.accepted_l0_count, 1)
    self.assertEqual(receipt.l2_event_ids, (self.expected_event_id(),))
    self.assert_counts(l0=1, l0_latest=1, l2=1, l2_latest=1, sources=1, outbox=1)

def test_injected_outbox_failure_rolls_back_every_business_write(self):
    self.repository.fail_at = "outbox"
    with self.assertRaisesRegex(DataTrunkError, "DATA_TRUNK_UNAVAILABLE"):
        self.trunk.ingest((self.raw_power(12345.0, sequence=1),))
    self.assert_counts(l0=0, l0_latest=0, l2=0, l2_latest=0, sources=0, outbox=0)
```

- [ ] **Step 6: 实现唯一事务 seam**

```python
class ConversionEvaluator(Protocol):
    def __call__(
        self,
        *,
        installed: tuple[InstalledPointConversion, ...],
        current_inputs: Mapping[InputReference, RawObservation | L2Observation],
        site_configuration_version: int,
        calculated_at: datetime,
    ) -> tuple[L2Observation, ...]:
        raise NotImplementedError


class DataTrunkRepository(Protocol):
    def transact(
        self,
        raw_observations: tuple[RawObservation, ...],
        evaluator: ConversionEvaluator,
    ) -> CommitReceipt:
        raise NotImplementedError


class DataTrunk:
    def __init__(self, repository: DataTrunkRepository) -> None:
        self._repository = repository

    def ingest(self, raw_observations: Sequence[RawObservation]) -> CommitReceipt:
        batch = tuple(raw_observations)
        if not batch:
            raise DataTrunkError("DATA_TRUNK_BATCH_EMPTY", "Raw observation batch is empty")
        return self._repository.transact(batch, evaluate_conversion)


class PostgresDataTrunkRepository:
    def transact(
        self,
        raw_observations: tuple[RawObservation, ...],
        evaluator: ConversionEvaluator,
    ) -> CommitReceipt:
        transaction_id = uuid4()
        with self._connection() as connection:
            with connection.cursor() as cursor:
                accepted = self._insert_l0(cursor, raw_observations)
                late_l0 = self._advance_l0_latest(cursor, accepted)
                snapshot = self._load_conversion_snapshot(cursor, accepted)
                produced = evaluator(
                    installed=snapshot.installed,
                    current_inputs=snapshot.current_inputs,
                    site_configuration_version=snapshot.site_configuration_version,
                    calculated_at=self._clock(),
                )
                self._insert_l2(cursor, produced)
                advanced = self._advance_l2_latest(cursor, produced)
                self._insert_sources(cursor, produced)
                self._insert_outbox(cursor, advanced)
            connection.commit()
        return CommitReceipt(
            transaction_id=transaction_id,
            accepted_l0_count=len(accepted),
            duplicate_l0_count=len(raw_observations) - len(accepted),
            l2_event_ids=tuple(item.event_id for item in produced),
            late_observation_count=late_l0,
        )
```

`source_order_key` 固定为 `S:<20位零填充sequence>:<digest>`，无 sequence 时为 `D:<digest>`。`_advance_l0_latest` 的 `ON CONFLICT ... DO UPDATE` 使用 `WHERE EXCLUDED.ts > t_telemetry_latest.ts OR (EXCLUDED.ts = t_telemetry_latest.ts AND EXCLUDED.source_order_key > t_telemetry_latest.source_order_key)`；`_advance_l2_latest` 对 `observed_at/source_order_key` 使用同一规则。仅 L2 `RETURNING event_id` 的实际推进行进入 outbox。

- [ ] **Step 7: 运行真实 PostgreSQL 原子性和幂等测试**

Run: `$env:ZIZU_POSTGRES_TEST='1'; cd backend; python -m unittest tests.test_data_trunk_postgres -v`

Expected: 正常提交、重复 source digest、outbox 故障、source relation 故障、late history/latest 条件更新全部 PASS。

- [ ] **Step 8: 提交 Task 2**

```bash
git add init-db/migration_038_pcs_data_trunk.sql backend/app/services/data_trunk.py backend/app/services/data_trunk_postgres.py backend/tests/test_data_trunk_migration_postgres.py backend/tests/test_data_trunk_postgres.py
git commit -m "feat(data): persist PCS trunk atomically"
```

---

### Task 3: 增加枚举、多故障码、四态质量、乱序和新鲜度

**Files:**
- Modify: `backend/app/services/data_trunk_contracts.py`
- Modify: `backend/app/services/data_trunk_conversion.py`
- Modify: `backend/app/services/data_trunk.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/tests/test_data_trunk_conversion.py`
- Modify: `backend/tests/test_data_trunk_postgres.py`

**Interfaces:**
- Consumes: Task 1/2 的 conversion snapshot 和事务 seam。
- Produces: `EnumTransform`、`FaultCodeTransform`、内部 `_FreshnessScheduler.run_once(now) -> int`；不增加第二个外部业务写 API。

- [ ] **Step 1: 写枚举、多码和质量 RED**

```python
def test_maps_operating_state_enum(self):
    output = self.evaluate_enum(raw="2", mapping={"0": "STOPPED", "2": "RUNNING"})
    self.assertEqual(output.value, TypedValue.enum("RUNNING"))
    self.assertEqual(output.quality, TrunkQuality.GOOD)

def test_unknown_enum_is_bad_without_current_value(self):
    output = self.evaluate_enum(raw="99", mapping={"0": "STOPPED"})
    self.assertEqual(output.value, TypedValue.enum(None))
    self.assertEqual((output.quality, output.reason), (TrunkQuality.BAD, "UNMAPPED_ENUM"))

def test_fault_codes_are_split_deduplicated_sorted_and_keep_unknown(self):
    output = self.evaluate_fault_codes(raw="E30; e11;E30;X99")
    self.assertEqual(output.value, TypedValue.code_set(("COMPRESSOR_FAULT", "DC_OVERVOLTAGE", "X99")))
    self.assertEqual((output.quality, output.reason), (TrunkQuality.UNCERTAIN, "UNMAPPED_FAULT_CODE"))

def test_out_of_range_numeric_is_bad_and_null(self):
    output = self.evaluate_power(raw_watts=900_000.0)
    self.assertEqual(output.value, TypedValue.float(None))
    self.assertEqual((output.quality, output.reason), (TrunkQuality.BAD, "OUT_OF_RANGE"))

def test_missing_required_input_emits_bad_output_instead_of_guessing(self):
    output = self.evaluate_without_required_state_input()
    self.assertEqual(output.value, TypedValue.enum(None))
    self.assertEqual((output.quality, output.reason), (TrunkQuality.BAD, "INPUT_MISSING"))

def test_runtime_type_mismatch_keeps_l0_but_emits_bad_l2(self):
    receipt = self.trunk.ingest((self.raw_power_text("not-a-number"),))
    self.assertEqual(receipt.accepted_l0_count, 1)
    self.assertEqual(self.l0_history_count(), 1)
    self.assertEqual((self.l2_latest_quality(), self.l2_latest_reason()), (TrunkQuality.BAD, "TYPE_MISMATCH"))
```

- [ ] **Step 2: 运行 conversion RED**

Run: `cd backend; python -m unittest tests.test_data_trunk_conversion -v`

Expected: FAIL，缺少 enum/code-set constructor 和 transform evaluator。

- [ ] **Step 3: 实现强类型 transform union**

```python
@dataclass(frozen=True)
class EnumTransform:
    input: InputReference
    entries: Mapping[str, str]


@dataclass(frozen=True)
class FaultCodeTransform:
    input: InputReference
    delimiter_pattern: str
    entries: Mapping[str, str]


Transform = NumericTransform | EnumTransform | FaultCodeTransform
```

将 `InstalledPointConversion.transform: NumericTransform` 精确改为 `transform: Transform`。在 `TypedValue` 类中增加：

```python
    @classmethod
    def enum(cls, value: str | None) -> "TypedValue":
        return cls(ValueKind.ENUM, value)

    @classmethod
    def code_set(cls, value: tuple[str, ...] | None) -> "TypedValue":
        return cls(ValueKind.CODE_SET, value)
```

`FaultCodeTransform` 只允许预编译的安全分隔符集合 `semicolon|comma|pipe|whitespace`，按 `strip→upper→remove empty→deduplicate→sort raw→map` 执行；不接受任意 regex。输入质量不是 GOOD 时输出质量不得优于最差输入；BAD/STALE 强制 typed value null。

- [ ] **Step 4: 写乱序、tie-breaker 和 STALE RED**

```python
def test_late_observation_is_historical_but_does_not_advance_latest_or_outbox(self):
    self.trunk.ingest((self.raw_power(20_000, at=self.t2, sequence=2),))
    receipt = self.trunk.ingest((self.raw_power(10_000, at=self.t1, sequence=1),))
    self.assertEqual(receipt.late_observation_count, 1)
    self.assertEqual(self.l2_history_count(), 2)
    self.assertEqual(self.l2_latest_value(), 20.0)
    self.assertEqual(self.outbox_count(), 1)

def test_freshness_scheduler_writes_stale_history_latest_source_and_outbox_atomically(self):
    self.trunk.ingest((self.raw_power(20_000, at=self.t1, sequence=1),))
    count = self.scheduler.run_once(self.t1 + timedelta(seconds=31))
    self.assertEqual(count, 1)
    self.assertEqual(self.l2_latest_quality(), TrunkQuality.STALE)
    self.assertIsNone(self.l2_latest_typed_value())
    self.assertEqual(self.outbox_count(), 2)
```

- [ ] **Step 5: 实现内部 freshness 调度**

```python
class _FreshnessScheduler:
    def __init__(
        self,
        repository: PostgresDataTrunkRepository,
        clock: Callable[[], datetime],
    ) -> None:
        self._repository = repository
        self._clock = clock

    def run_once(self, now: datetime | None = None) -> int:
        return self._repository.mark_expired_outputs_stale(now or self._clock())
```

`mark_expired_outputs_stale()` 复用同一个内部 transaction executor；锁定 expired current rows，生成稳定 STALE event ID，在一个事务内写 L2 history/latest/source (`source_kind='freshness'`) 和 outbox。相同实体、原 event、deadline 的重复扫描必须幂等。

- [ ] **Step 6: 运行 Task 3 定向测试**

Run: `cd backend; python -m unittest tests.test_data_trunk_conversion -v`

Run: `$env:ZIZU_POSTGRES_TEST='1'; cd backend; python -m unittest tests.test_data_trunk_postgres -v`

Expected: enum、多码、BAD/UNCERTAIN/STALE、late/tie-breaker、freshness rollback 全部 PASS。

- [ ] **Step 7: 提交 Task 3**

```bash
git add backend/app/services/data_trunk_contracts.py backend/app/services/data_trunk_conversion.py backend/app/services/data_trunk.py backend/app/services/data_trunk_postgres.py backend/tests/test_data_trunk_conversion.py backend/tests/test_data_trunk_postgres.py
git commit -m "feat(data): enforce PCS quality and time semantics"
```

---
### Task 4: 将现有 pipeline 切到 receipt 后确认的 raw ingestion

**Files:**
- Modify: `backend/app/services/pipeline.py`
- Modify: `backend/app/services/telemetry_store.py`
- Modify: `backend/app/services/data_trunk.py`
- Create: `backend/tests/test_pipeline_data_trunk.py`
- Modify: `backend/tests/postgres_delivery_app.py`

**Interfaces:**
- Consumes: `DataTrunk.ingest()` 和 Task 1 的 `RawObservation`。
- Produces: `RawObservationAdapter.from_parsed(parsed, tag_catalog) -> tuple[RawObservation, ...]`；兼容投影仅供未迁移 tag reader，不是 L1 输入。

- [ ] **Step 1: 写 buffer 不得提前清除的 RED**

```python
class PipelineDataTrunkTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_ingest_keeps_exact_buffer_prefix_for_retry(self):
        trunk = RecordingDataTrunk(fail_once=True)
        pipeline = self.pipeline(trunk)
        await pipeline.on_message(self.neuron_message(power=12345))

        await pipeline.flush_now()
        self.assertEqual(pipeline.buffer_observation_ids(), (self.expected_observation_id(),))

        await pipeline.flush_now()
        self.assertEqual(pipeline.buffer_observation_ids(), ())
        self.assertEqual(trunk.calls[0], trunk.calls[1])

    async def test_concurrent_append_is_not_removed_with_committed_prefix(self):
        trunk = BlockingDataTrunk()
        pipeline = self.pipeline(trunk)
        await pipeline.on_message(self.neuron_message(power=1000, sequence=1))
        flush = asyncio.create_task(pipeline.flush_now())
        await trunk.started.wait()
        await pipeline.on_message(self.neuron_message(power=2000, sequence=2))
        trunk.release.set()
        await flush
        self.assertEqual(pipeline.buffer_sequences(), (2,))
```

- [ ] **Step 2: 运行 pipeline RED**

Run: `cd backend; python -m unittest tests.test_pipeline_data_trunk -v`

Expected: 第一项失败，当前 `_do_flush()` 在数据库调用前执行 `self._buffer.clear()`。

- [ ] **Step 3: parser 后生成 canonical raw observation**

```python
@dataclass(frozen=True)
class TagMetadata:
    node_id: UUID
    tag_id: UUID
    stable_source_key: str
    data_type: str
    unit: str | None


class RawObservationAdapter:
    def from_parsed(
        self,
        parsed: ParsedMessage,
        tag_catalog: Mapping[str, TagMetadata],
        *,
        received_at: datetime,
        source_message_id: str | None,
        source_sequence: int | None,
    ) -> tuple[RawObservation, ...]:
        return tuple(
            RawObservation(
                observation_id=_observation_id(parsed, source_key, raw_value, source_sequence),
                node_id=tag_catalog[source_key].node_id,
                tag_id=tag_catalog[source_key].tag_id,
                source_key=tag_catalog[source_key].stable_source_key,
                value=_raw_typed_value(raw_value, tag_catalog[source_key].data_type),
                raw_unit=tag_catalog[source_key].unit,
                quality=TrunkQuality.GOOD,
                source_timestamp=parsed.timestamp,
                received_at=received_at,
                source_message_id=source_message_id,
                source_sequence=source_sequence,
                source_digest=_source_digest(parsed, source_key, raw_value, source_sequence),
            )
            for source_key, raw_value in sorted(parsed.tags.items())
            if source_key in tag_catalog and isinstance(raw_value, (float, int, bool, str))
        )
```

`_source_digest` 只包含 node stable key、tag stable key、source timestamp、sequence 和 canonical typed value；不含 broker 地址、topic 凭据或 Secret。`normalizer.py` 的 scale/offset/range 只通过 `LegacyTelemetryProjection` 产生既有 `value_*` 兼容列；新 L1 从 `raw_value_*` 读取。

- [ ] **Step 4: 修改 `_do_flush()` 为 receipt 后删除前缀**

```python
async def _do_flush(self) -> None:
    async with self._buffer_lock:
        batch = tuple(self._buffer[: self._flush_batch_size])
    if not batch:
        return
    try:
        receipt = await asyncio.to_thread(self._data_trunk.ingest, batch)
    except DataTrunkError as exc:
        self.metrics.db_write_errors += 1
        self._record_retry(exc, batch)
        return
    async with self._buffer_lock:
        committed_ids = tuple(item.observation_id for item in batch)
        current_ids = tuple(item.observation_id for item in self._buffer[: len(batch)])
        if current_ids != committed_ids:
            raise RuntimeError("pipeline buffer prefix changed during ingest")
        del self._buffer[: len(batch)]
    self.metrics.points_written_db += receipt.accepted_l0_count
```

模块常量 `MAX_INGEST_ATTEMPTS = 5`，退避固定为 `0.25/0.5/1/2/4` 秒。第 5 次失败后调用 repository 的独立 `record_failure()` 事务，成功写入 failure reference 后才移除该批；该函数只存安全摘要和 source digest。

- [ ] **Step 5: 删除 pipeline 对旧双提交写路径的调用**

静态测试必须锁定生产路径没有下列调用：

```python
def test_pipeline_has_one_business_write_seam(self):
    source = inspect.getsource(DataPipeline._do_flush)
    self.assertNotIn("batch_insert_telemetry", source)
    self.assertNotIn("upsert_telemetry_latest", source)
    self.assertEqual(source.count("self._data_trunk.ingest"), 1)
```

保留 `telemetry_store.query_telemetry/query_latest_values`；`batch_insert_telemetry/upsert_telemetry_latest` 标记为 legacy test helper，生产 `DataPipeline` 不再导入。

- [ ] **Step 6: 让公共协议模拟器通过 pipeline 进入 DataTrunk**

`backend/tests/postgres_delivery_app.py` 的 `/protocol-simulator/neuron` 仍调用真实 `DataPipeline.on_message()` 和 `flush_now()`，但 app fixture 注入 `build_postgres_data_trunk()`；测试不得直写 `t_telemetry` 或 `t_l2_observations`。

- [ ] **Step 7: 运行 pipeline 与既有协议回归**

Run: `cd backend; python -m unittest tests.test_pipeline_data_trunk tests.test_parser tests.test_normalizer tests.test_tag_mqtt_alarm_adapter_contract -v`

Run: `$env:ZIZU_POSTGRES_TEST='1'; cd backend; python -m unittest tests.test_data_trunk_postgres -v`

Expected: buffer retry/concurrent append、raw columns、legacy compatibility projection、原子主缝全部 PASS。

- [ ] **Step 8: 提交 Task 4**

```bash
git add backend/app/services/pipeline.py backend/app/services/telemetry_store.py backend/app/services/data_trunk.py backend/tests/test_pipeline_data_trunk.py backend/tests/postgres_delivery_app.py
git commit -m "refactor(data): route ingestion through DataTrunk"
```

---

### Task 5: 发布两套 PCS 点位转换资产并实现确定性 plan/apply 领域行为

**Files:**
- Create: `backend/app/services/solution_point_conversions.py`
- Create: `backend/app/services/point_conversion.py`
- Modify: `backend/app/services/solution_package_archive.py`
- Modify: `backend/app/services/solution_delivery.py`
- Create: `backend/tests/test_solution_point_conversions.py`
- Create: `backend/tests/test_point_conversion.py`
- Create: `reference-deliveries/pv-storage-charging-ems/point-conversions/pcs-brand-a.yaml`
- Create: `reference-deliveries/pv-storage-charging-ems/point-conversions/pcs-brand-b.yaml`
- Create: `reference-deliveries/pv-storage-charging-ems/entities/pcs-operating-state.yaml`
- Create: `reference-deliveries/pv-storage-charging-ems/entities/pcs-fault-codes.yaml`
- Modify: `reference-deliveries/pv-storage-charging-ems/package.yaml`
- Modify: `reference-deliveries/pv-storage-charging-ems/slots/pcs.yaml`
- Modify: `reference-deliveries/pv-storage-charging-ems/README.md`

**Interfaces:**
- Consumes: migration 038 的关系模型和 Task 1 transform types。
- Produces: `PointConversion.plan(PlanPointConversion) -> PointConversionPlan`、`get_plan(UUID)`、`apply(ApplyPointConversionPlan) -> PointConversionApplication`；validated `PointConversionAsset`。

- [ ] **Step 1: 写包资产格式 RED**

```python
def test_imports_two_pcs_templates_with_same_three_outputs(self):
    package = self.delivery.import_package(build_reference_archive())
    assets = point_conversion_assets(package)
    self.assertEqual({item.asset_id for item in assets}, {"pcs.brand-a", "pcs.brand-b"})
    for asset in assets:
        self.assertEqual(
            tuple(output.entity_definition_id for output in asset.outputs),
            ("pcs.active_power", "pcs.fault_codes", "pcs.operating_state"),
        )

def test_rejects_arbitrary_expression_rule(self):
    with self.assertRaisesRegex(DeliveryError, "POINT_CONVERSION_RULE_INVALID"):
        self.delivery.import_package(self.archive_with_rule({"kind": "expression", "code": "eval(raw)"}))
```

- [ ] **Step 2: 运行资产 RED**

Run: `cd backend; python -m unittest tests.test_solution_point_conversions -v`

Expected: FAIL，archive whitelist 不认识 `point_conversion_template`。

- [ ] **Step 3: 添加精确 YAML Schema**

Brand A 资产使用：

```yaml
schemaVersion: zizu.point-conversion/v1alpha1
id: pcs.brand-a
kind: point_conversion_template
displayName: PCS 通用品牌 A
deviceCategory: PCS
brand: GENERIC_A
model: PCS-A
revision: 1
status: active
inputs:
  - {id: active_power_raw, sourceKind: l0, sourceKey: ActivePowerRaw, aliases: [ActivePower], dataType: FLOAT, unit: W, required: true}
  - {id: operating_state_raw, sourceKind: l0, sourceKey: RunningState, aliases: [RunState], dataType: STRING, required: true}
  - {id: fault_codes_raw, sourceKind: l0, sourceKey: FaultCodeText, aliases: [FaultCodes], dataType: STRING, required: true}
outputs:
  - id: active_power
    entityDefinition: pcs.active_power
    dataType: FLOAT
    unit: kW
    freshness: 30s
    transform: {kind: numeric, input: active_power_raw, scale: 0.001, offset: 0, minimum: -500, maximum: 500}
  - id: operating_state
    entityDefinition: pcs.operating_state
    dataType: ENUM
    freshness: 30s
    transform:
      kind: enum
      input: operating_state_raw
      entries: {"0": STOPPED, "1": STANDBY, "2": RUNNING, "3": FAULTED}
  - id: fault_codes
    entityDefinition: pcs.fault_codes
    dataType: CODE_SET
    freshness: 30s
    transform:
      kind: fault_codes
      input: fault_codes_raw
      delimiter: semicolon
      entries:
        E11: {code: DC_OVERVOLTAGE, name: 直流过压, defaultSeverity: MAJOR}
        E30: {code: COMPRESSOR_FAULT, name: 压缩机故障, defaultSeverity: WARNING}
```

Brand B 使用以下完整差异，输出定义保持不变：

```yaml
schemaVersion: zizu.point-conversion/v1alpha1
id: pcs.brand-b
kind: point_conversion_template
displayName: PCS 通用品牌 B
deviceCategory: PCS
brand: GENERIC_B
model: PCS-B
revision: 1
status: active
inputs:
  - {id: active_power_raw, sourceKind: l0, sourceKey: PActKw, aliases: [ActivePwrKw], dataType: FLOAT, unit: kW, required: true}
  - {id: operating_state_raw, sourceKind: l0, sourceKey: ModeCode, aliases: [OperatingMode], dataType: STRING, required: true}
  - {id: fault_codes_raw, sourceKind: l0, sourceKey: AlarmList, aliases: [ActiveAlarms], dataType: STRING, required: true}
outputs:
  - id: active_power
    entityDefinition: pcs.active_power
    dataType: FLOAT
    unit: kW
    freshness: 30s
    transform: {kind: numeric, input: active_power_raw, scale: 1, offset: 0, minimum: -500, maximum: 500}
  - id: operating_state
    entityDefinition: pcs.operating_state
    dataType: ENUM
    freshness: 30s
    transform:
      kind: enum
      input: operating_state_raw
      entries: {S: STOPPED, W: STANDBY, R: RUNNING, F: FAULTED}
  - id: fault_codes
    entityDefinition: pcs.fault_codes
    dataType: CODE_SET
    freshness: 30s
    transform:
      kind: fault_codes
      input: fault_codes_raw
      delimiter: comma
      entries:
        D11: {code: DC_OVERVOLTAGE, name: 直流过压, defaultSeverity: MAJOR}
        C30: {code: COMPRESSOR_FAULT, name: 压缩机故障, defaultSeverity: WARNING}
```

实体定义 `pcs.operating_state` 为 `ENUM/R`，`pcs.fault_codes` 为 `CODE_SET/R`；slot 的三项读取实体声明 `sourceKind: point_conversion` 和对应 `conversionOutputKey`，控制实体继续使用已有独立 direct control matcher。

`package.yaml.assets` 增加四行：

```yaml
  - {id: pcs.operating_state, kind: entity_definition, path: entities/pcs-operating-state.yaml}
  - {id: pcs.fault_codes, kind: entity_definition, path: entities/pcs-fault-codes.yaml}
  - {id: pcs.brand-a, kind: point_conversion_template, path: point-conversions/pcs-brand-a.yaml}
  - {id: pcs.brand-b, kind: point_conversion_template, path: point-conversions/pcs-brand-b.yaml}
```

`slots/pcs.yaml.requiredEntities` 中读取实体精确使用：

```yaml
  - {definition: pcs.active_power, sourceKind: point_conversion, conversionOutputKey: active_power}
  - {definition: pcs.operating_state, sourceKind: point_conversion, conversionOutputKey: operating_state}
  - {definition: pcs.fault_codes, sourceKind: point_conversion, conversionOutputKey: fault_codes}
```

既有 `pcs.setpoint/pcs.readback/bms.ready` 控制与联锁声明原样保留，不用点位转换反推写地址。

- [ ] **Step 4: 实现严格 asset validator**

```python
def parse_point_conversion_asset(raw: Mapping[str, Any]) -> PointConversionAsset:
    _require_exact_schema(raw, "zizu.point-conversion/v1alpha1")
    inputs = _parse_unique_inputs(raw["inputs"])
    outputs = _parse_unique_outputs(raw["outputs"], inputs)
    if raw["deviceCategory"] != "PCS":
        raise PointConversionAssetError("POINT_CONVERSION_DEVICE_CATEGORY_UNSUPPORTED")
    return PointConversionAsset.from_validated(raw, inputs, outputs)
```

Validator 固定接受 `numeric|enum|fault_codes`；numeric 所有数值必须 `math.isfinite`；enum/fault raw key 唯一；output entity/data type/unit 必须与实体定义一致；L0 input 只可绑定同节点，L2 input 只可绑定已确认实体；`status` 只允许 `active|retired`。retired revision 仍需携带完整不可变契约，但不允许新 plan 选择，已安装 revision 不受影响。

- [ ] **Step 5: 写确定性 plan/apply RED**

```python
def test_plan_blocks_missing_and_ambiguous_required_inputs(self):
    plan = self.service.plan(self.request(tags=self.tags_without_fault_and_two_state_candidates()))
    self.assertEqual(plan.status, "blocked")
    self.assertEqual(
        {item["code"] for item in plan.blockers},
        {"POINT_CONVERSION_INPUT_MISSING", "POINT_CONVERSION_INPUT_AMBIGUOUS"},
    )
    self.assertEqual(self.repository.application_count(), 0)

def test_brand_replacement_preserves_output_entity_ids(self):
    first = self.apply(self.plan(template="pcs.brand-a"))
    second = self.apply(self.plan(template="pcs.brand-b"))
    self.assertNotEqual(first.revision_id, second.revision_id)
    self.assertEqual(first.output_entity_instance_ids, second.output_entity_instance_ids)
```

- [ ] **Step 6: 实现领域 API 和稳定计划项**

```python
@dataclass(frozen=True)
class PlanPointConversion:
    node_id: UUID
    template_revision_id: UUID
    input_selections: Mapping[str, UUID]
    actor: str
    entity_identity_installation_id: UUID | None = None
    planned_output_entity_ids: Mapping[str, UUID] = field(default_factory=dict)
    solution_installation_id: UUID | None = None


@dataclass(frozen=True)
class ApplyPointConversionPlan:
    plan_id: UUID
    plan_digest: str
    idempotency_key: str
    actor: str


class PointConversionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PointConversionPlan:
    id: UUID
    node_id: UUID
    template_revision_id: UUID
    entity_identity_installation_id: UUID
    solution_installation_id: UUID
    base_site_configuration_version: int
    source_catalog_digest: str
    status: str
    items: tuple[dict[str, Any], ...]
    blockers: tuple[dict[str, str], ...]
    digest: str
    planned_by: str


@dataclass(frozen=True)
class PointConversionApplication:
    id: UUID
    plan_id: UUID
    installed_conversion_id: UUID
    solution_installation_id: UUID
    revision_id: UUID
    site_configuration_version: int
    output_entity_instance_ids: tuple[UUID, ...]
    actor: str


class PointConversion:
    def plan(self, command: PlanPointConversion) -> PointConversionPlan:
        plan = compile_point_conversion_plan(command, self._templates, self._sources, self._repository)
        return self._repository.save_plan(plan)

    def get_plan(self, plan_id: UUID) -> PointConversionPlan:
        plan = self._repository.get_plan(plan_id)
        if plan is None:
            raise PointConversionError("POINT_CONVERSION_PLAN_NOT_FOUND", "Point conversion plan was not found")
        return plan

    def apply(self, command: ApplyPointConversionPlan, *, transaction: Any | None = None) -> PointConversionApplication:
        return self._repository.apply_plan(command, transaction=transaction)
```

Plan 对 input candidates 先按 stable source key，再按 alias，随后验证 node/type/unit；明确 selection 必须属于候选集。首次 solution plan 由外层传入 `planned_output_entity_ids`，独立换牌时 repository 从当前 output bindings 读取同一映射；二者都必须与 entity identity installation、device instance stable key、definition id 的稳定 UUID 计算一致，且不包含 brand/revision。items 固定 `add|update|preserve|delete_candidate|block`；digest 覆盖 base site version、catalog digest、revision digest、输入 selection、输出 entity IDs 和 items。

`PointConversionAsset` 是 `asset_id/device_category/brand/model/revision/content_digest/inputs/outputs` 的 frozen dataclass；inputs/outputs 均为 tuple，嵌套 mapping 在 parser 入口递归冻结。`compile_point_conversion_plan(command, templates, sources, repository)` 返回上述 `PointConversionPlan`，不得在编译阶段写 installed/application/site version。首次 solution plan 把目标 installation ID 写入 `solution_installation_id`；独立换牌 plan 先读取当前 solution installation，并在 apply 时生成新的 derived solution installation。

- [ ] **Step 7: 运行资产与领域测试**

Run: `cd backend; python -m unittest tests.test_solution_point_conversions tests.test_point_conversion -v`

Expected: schema 正负例、Brand A/B 自动匹配率均为 100%（高于 95% 门槛）、必需输入 100% 门禁、换牌稳定 ID、same actor/key replay 全部 PASS。

- [ ] **Step 8: 提交 Task 5**

```bash
git add backend/app/services/solution_point_conversions.py backend/app/services/point_conversion.py backend/app/services/solution_package_archive.py backend/app/services/solution_delivery.py backend/tests/test_solution_point_conversions.py backend/tests/test_point_conversion.py reference-deliveries/pv-storage-charging-ems
git commit -m "feat(data): add versioned PCS conversion assets"
```

---

### Task 6: 将 point conversion 接入解决方案安装、PostgreSQL 和公开 REST/RBAC

**Files:**
- Create: `backend/app/services/point_conversion_postgres.py`
- Create: `backend/app/api/point_conversions.py`
- Modify: `backend/app/services/solution_delivery_contracts.py`
- Modify: `backend/app/services/solution_delivery.py`
- Modify: `backend/app/services/solution_delivery_repository.py`
- Modify: `backend/app/services/entity_instance_registry.py`
- Modify: `backend/app/api/solution_delivery.py`
- Modify: `backend/app/api/business_security.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_point_conversion_public_api.py`
- Create: `backend/tests/test_point_conversion_postgres.py`
- Modify: `backend/tests/test_business_rest_authorization.py`

**Interfaces:**
- Consumes: Task 5 的 plan/apply、既有 `SolutionDeliveryRepository.install(plan, actor, key, request_digest, apply_configuration)` 单事务 callback。
- Produces: `PointConversionSelection` request DTO；五个 point-conversion REST 路由；首次安装和独立换牌共用同一个 repository apply seam。

- [ ] **Step 1: 写公开 REST/RBAC RED**

```python
def test_point_conversion_public_matrix(self):
    self.assert_status("GET", "/api/v1/point-conversion-templates?device_category=PCS", anonymous=401, operator=403, engineer=200, admin=200)
    self.assert_status("GET", f"/api/v1/nodes/{self.node_id}/data-trunk", anonymous=401, operator=200, engineer=200, admin=200)
    self.assert_status("POST", f"/api/v1/nodes/{self.node_id}/point-conversion-plans", anonymous=401, operator=403, engineer=201, admin=201)
    self.assert_status("POST", f"/api/v1/point-conversion-plans/{self.plan_id}/apply", anonymous=401, operator=403, engineer=201, admin=201)

def test_denied_apply_has_audit_and_zero_configuration_write(self):
    response = self.operator.post(self.apply_path, headers={"Idempotency-Key": "operator-denied"}, json={"plan_digest": "a" * 64})
    self.assertEqual(response.status_code, 403)
    self.assertEqual(self.repository.application_count(), 0)
    self.assert_authorization_denied_audit("point_conversion.apply")

def test_operator_node_trunk_projection_contains_l2_but_not_l0_or_l1_details(self):
    body = self.operator.get(f"/api/v1/nodes/{self.node_id}/data-trunk").json()
    self.assertTrue(body["l2"])
    self.assertEqual(body["l0"], [])
    self.assertNotIn("input_bindings", body["l1_summary"])
```

- [ ] **Step 2: 运行 public RED**

Run: `cd backend; python -m unittest tests.test_point_conversion_public_api -v`

Expected: 404，router 尚未注册。

- [ ] **Step 3: 添加强类型 solution install request**

```python
class PointConversionSelection(BaseModel):
    node_id: UUID
    template_revision_id: UUID
    input_selections: dict[str, UUID] = Field(default_factory=dict)


class CreateInstallationPlanRequest(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)
    secret_references: dict[str, str] = Field(default_factory=dict)
    binding_selections: dict[str, UUID] = Field(default_factory=dict)
    binding_overrides: dict[str, UUID] = Field(default_factory=dict)
    upgrade_risk_resolutions: dict[str, str] = Field(default_factory=dict)
    point_conversions: list[PointConversionSelection] = Field(default_factory=list, max_length=8)


class PointConversionPlanRequest(BaseModel):
    template_revision_id: UUID
    input_selections: dict[str, UUID] = Field(default_factory=dict, max_length=64)

    def to_command(self, *, node_id: UUID, actor: str) -> PlanPointConversion:
        return PlanPointConversion(
            node_id=node_id,
            template_revision_id=self.template_revision_id,
            input_selections=self.input_selections,
            actor=actor,
        )


class ApplyPointConversionRequest(BaseModel):
    plan_digest: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
```

`InstallationPlan` 新增 `point_conversion_plans: tuple[dict[str, Any], ...] = ()`，必须进入 plan digest、PG JSON 列和 public response。`EntityInstanceRegistry.plan()` 对 `sourceKind=point_conversion` 只创建稳定 device/entity items，不搜索 tag candidates、不创建 `t_entity_instance_bindings`；其 output binding 由 point-conversion 子计划负责。

`SolutionDelivery.plan_install()` 的顺序固定为：先从 `pcs.instances` 和 `PointConversionSelection.node_id` 验证一一对应的 PCS device instance；再生成 entity plan 及未来稳定 entity IDs；随后把 `{conversionOutputKey: entity_instance_id}` 作为 `planned_output_entity_ids` 编译 point-conversion plan；最后把两个子计划的 digest 同时写入 installation plan digest。任何 node/slot/template/output 对不上都生成 installation blocker，不保存半个子计划、不提前创建 entity row。

每个 `template_revision_id` 必须通过 `t_solution_point_conversion_assets(package_record_id, template_revision_id)` 证明属于当前解决方案包；不得从全局 catalog 选择另一个包的 revision。独立换牌只允许同 package family、同 device category、输出定义集合完全一致的 revision，否则形成 `POINT_CONVERSION_OUTPUT_CONTRACT_MISMATCH` blocker。

`PostgresDeliveryRepository.save_package()` 在保存 package/assets 的同一 transaction 中调用 `persist_point_conversion_assets(cursor, package)`，把 validated template/revision/input/output/rule/mapping 关系写入 migration 038 表。相同 `(template,revision)` 的 digest 一致时幂等，digest 不同稳定拒绝 `POINT_CONVERSION_REVISION_CONFLICT`；任一 relation INSERT 失败时 package、asset bytes 和 L1 catalog 全部回滚。

Template identity 的唯一可变字段是 `status`：admin 导入含更高 immutable revision 的 `status: retired` 资产时，在 package transaction 内更新 template status 并追加 `point_conversion.template_status` 审计；engineer/operator 没有 package import capability。retired template 不出现在新计划候选中，但现有 installed row、历史 L2 和来源证据保持可读。

为保证该审计主体真实，Task 6 同步把公开 seam 改为 `SolutionDelivery.import_package(archive: bytes, actor: str)` 和 `DeliveryRepository.save_package(package, actor)`；`import_solution_package` 传 `principal.actor`，不再 `del principal`。existing package import tests 传明确 actor，repository 在同一 transaction 写 package、L1 catalog 和 template status audit。

- [ ] **Step 4: 让首次安装共享既有事务 callback**

```python
def apply_configuration(transaction: Any | None) -> tuple[UUID, ...]:
    entity_ids = apply_entities(transaction)
    for point_conversion_plan in sorted(point_conversion_plans, key=lambda item: item["node_id"]):
        self._point_conversions.apply(
            ApplyPointConversionPlan(
                plan_id=UUID(point_conversion_plan["id"]),
                plan_digest=point_conversion_plan["digest"],
                idempotency_key=f"solution:{idempotency_key}",
                actor=actor,
            ),
            transaction=transaction,
        )
    if alarm_plan is not None:
        self._alarm_definitions.install_definitions(_alarm_plan_from_dict(alarm_plan), transaction)
    return entity_ids
```

`SolutionDeliveryRepository.install()` 已把 callback 放在 `t_site_configuration_state FOR UPDATE` 和最终 commit 之间；测试故障注入 point conversion output binding INSERT，必须证明 entity instances、solution installation、site version、audit 和 point conversion 全部为零。

- [ ] **Step 5: 实现 PostgreSQL plan/apply 锁与幂等**

`PostgresPointConversionRepository.apply_plan()` 使用传入 connection 时不得 commit；独立换牌时自己开启事务。两种路径均锁 `t_site_configuration_state`、plan row、当前 installed row；复核 catalog digest、base site version、plan digest、input candidate set 和 output entity IDs。独立换牌推进 site configuration version并写 `configuration.change` 审计；首次 solution install 由外层推进版本，子模块只用目标版本写 bindings。

独立换牌必须沿用 `alarm_configuration_postgres.py` 已验证的 lineage 模式：在同一事务内创建 canonical items/digest 明确含 `kind=point_conversion` 的 derived solution plan、derived solution installation 和下一版 `t_site_configuration_versions`，再让 point-conversion application 的 `solution_installation_id/site_configuration_version` 指向它；首次 solution install 则指向外层 target installation（deferrable FK 在外层随后插入时满足）。因此换牌后的 UI 和 `/acceptance-runs` 使用 application 返回的 `solution_installation_id`，报告锁定的版本不会停留在品牌 A。

- [ ] **Step 6: 实现公开路由和稳定错误映射**

```python
def get_point_conversions() -> PointConversion:
    return _point_conversions


def _raise_point_conversion_http(exc: PointConversionError) -> NoReturn:
    status_by_code = {
        "POINT_CONVERSION_PLAN_NOT_FOUND": 404,
        "POINT_CONVERSION_SELECTION_INVALID": 422,
        "POINT_CONVERSION_PLAN_STALE": 409,
        "POINT_CONVERSION_PLAN_DIGEST_MISMATCH": 409,
        "IDEMPOTENCY_KEY_REUSED": 409,
        "DATA_TRUNK_UNAVAILABLE": 503,
    }
    raise HTTPException(
        status_code=status_by_code.get(exc.code, 422),
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


@router.get("/point-conversion-templates", **protected(CONFIGURATION_READ))
async def list_point_conversion_templates(
    device_category: str,
    service: PointConversion = Depends(get_point_conversions),
) -> dict:
    items = service.list_templates(device_category.upper())
    return {"items": [item.public_dict() for item in items], "total": len(items)}

@router.get("/nodes/{node_id}/data-trunk", **protected(RUNTIME_READ))
async def read_node_data_trunk(
    node_id: UUID,
    principal: Principal = Depends(principal_for(RUNTIME_READ)),
    service: PointConversion = Depends(get_point_conversions),
) -> dict:
    return service.inspect_node(node_id, include_engineering=principal.role in {"admin", "engineer"}).public_dict()

@router.post("/nodes/{node_id}/point-conversion-plans", status_code=201, **protected(CONFIGURATION_WRITE))
async def create_point_conversion_plan(
    node_id: UUID,
    body: PointConversionPlanRequest,
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    service: PointConversion = Depends(get_point_conversions),
) -> dict:
    try:
        return service.plan(body.to_command(node_id=node_id, actor=principal.actor)).public_dict()
    except PointConversionError as exc:
        _raise_point_conversion_http(exc)

@router.get("/point-conversion-plans/{plan_id}", **protected(CONFIGURATION_READ))
async def read_point_conversion_plan(
    plan_id: UUID,
    service: PointConversion = Depends(get_point_conversions),
) -> dict:
    try:
        return service.get_plan(plan_id).public_dict()
    except PointConversionError as exc:
        _raise_point_conversion_http(exc)

@router.post("/point-conversion-plans/{plan_id}/apply", status_code=201, **protected(CONFIGURATION_WRITE))
async def apply_point_conversion_plan(
    plan_id: UUID,
    body: ApplyPointConversionRequest,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    principal: Principal = Depends(principal_for(CONFIGURATION_WRITE)),
    service: PointConversion = Depends(get_point_conversions),
) -> dict:
    command = ApplyPointConversionPlan(plan_id, body.plan_digest, idempotency_key, principal.actor)
    try:
        return service.apply(command).public_dict()
    except PointConversionError as exc:
        _raise_point_conversion_http(exc)
```

映射固定：missing/ambiguous/type/unit 为 plan 201+blocked；not found 404；invalid selection 422；stale/digest mismatch/key reused 409；repository unavailable 503。错误使用既有 `detail.code/message` 包络。

- [ ] **Step 7: 写真实 PostgreSQL 首装、换牌、并发与回滚测试**

```python
def test_solution_install_creates_entity_and_conversion_in_one_transaction(self):
    outcome = self.apply_solution(self.plan_solution(template="pcs.brand-a"))
    self.assertEqual(self.entity_count(), 3)
    self.assertEqual(self.current_conversion_revision(), self.brand_a_revision_id)
    self.assertEqual(outcome.site_configuration_version, self.current_site_version())

def test_brand_b_replacement_keeps_entity_ids_and_upper_references(self):
    before = self.install_brand_a_and_read_entity_ids()
    application = self.apply_point_conversion(self.plan_brand_b())
    self.assertEqual(self.read_entity_ids(), before)
    self.assertEqual(self.read_upper_consumer_entity_ids(), before)
    self.assertEqual(self.solution_installation_site_version(application.solution_installation_id), application.site_configuration_version)

def test_concurrent_same_plan_allows_one_apply_and_one_stale(self):
    outcomes = self.apply_from_two_connections(self.plan_brand_b())
    self.assertEqual(sorted(item.code for item in outcomes), ["APPLIED", "POINT_CONVERSION_PLAN_STALE"])

def test_output_binding_failure_rolls_back_solution_entity_site_version_and_audit(self):
    before = self.database_counts()
    with self.injected_failure("conversion_output_binding"):
        with self.assertRaisesRegex(PointConversionError, "DATA_TRUNK_UNAVAILABLE"):
            self.apply_solution(self.plan_solution(template="pcs.brand-a"))
    self.assertEqual(self.database_counts(), before)

def test_template_catalog_failure_rolls_back_package_and_all_l1_relations(self):
    before = self.database_counts()
    with self.injected_failure("enum_mapping_entry"):
        with self.assertRaisesRegex(DeliveryError, "POINT_CONVERSION_CATALOG_UNAVAILABLE"):
            self.import_reference_package()
    self.assertEqual(self.database_counts(), before)

def test_admin_retirement_hides_new_plan_candidate_but_keeps_installed_revision(self):
    installed = self.install_brand_a()
    self.admin_imports_retired_brand_a_revision_2()
    self.assertNotIn(self.brand_a_template_id, self.list_active_templates())
    self.assertEqual(self.current_installed_conversion_id(), installed.id)
    self.assert_audit("point_conversion.template_status", outcome="allowed")
```

Run: `$env:ZIZU_POSTGRES_TEST='1'; cd backend; python -m unittest tests.test_point_conversion_postgres -v`

Expected: 四项全部 PASS，且 same actor/key replay 返回同 application id。

- [ ] **Step 8: 运行 public、RBAC 和 delivery 回归**

Run: `cd backend; python -m unittest tests.test_point_conversion_public_api tests.test_business_rest_authorization tests.test_delivery_public_api -v`

Expected: 新增路由全部被 OpenAPI capability coverage 分类，既有路由基数按实际新增数量更新。

- [ ] **Step 9: 提交 Task 6**

```bash
git add backend/app/services/point_conversion_postgres.py backend/app/api/point_conversions.py backend/app/services/solution_delivery_contracts.py backend/app/services/solution_delivery.py backend/app/services/solution_delivery_repository.py backend/app/services/entity_instance_registry.py backend/app/api/solution_delivery.py backend/app/api/business_security.py backend/app/main.py backend/tests/test_point_conversion_public_api.py backend/tests/test_point_conversion_postgres.py backend/tests/test_business_rest_authorization.py
git commit -m "feat(data): install PCS conversion plans"
```

---

### Task 7: 让 EntityInstanceRuntime 从 L2 读取实时/历史且禁止 migrated PCS fallback

**Files:**
- Modify: `backend/app/services/entity_instance_registry.py`
- Modify: `backend/app/services/entity_instance_runtime.py`
- Modify: `backend/app/services/entity_instance_postgres.py`
- Modify: `backend/app/services/entity_instance_catalog.py`
- Modify: `backend/app/api/entity_instances.py`
- Modify: `backend/tests/test_entity_delivery_public_api.py`
- Create: `backend/tests/test_entity_instance_l2_runtime.py`

**Interfaces:**
- Consumes: `t_l2_latest/t_l2_observations`、point-conversion output binding、既有 `EntityInstanceRuntime.read/read_for_alarm/history`。
- Produces: source-kind aware `ResolvedEntitySource` 和 `GET /api/v1/entity-instances/{id}/history?range=<1h|6h|24h|7d>`；上层调用签名保持不变。

- [ ] **Step 1: 写 L2 read/history 和 no-fallback RED**

```python
def test_point_conversion_entity_reads_l2_latest_and_history(self):
    runtime = self.runtime_for_point_conversion_entity()
    latest = runtime.read(self.entity_id)
    history = runtime.history(self.entity_id, "1h")
    self.assertEqual(latest.value, 12.345)
    self.assertEqual(latest.source_kind, "point_conversion")
    self.assertEqual(latest.conversion_revision_id, self.revision_id)
    self.assertEqual(len(history), 2)

def test_missing_l2_does_not_fall_back_to_legacy_tag(self):
    self.seed_old_direct_tag_value(self.entity_id, 99.0)
    self.remove_l2_latest(self.entity_id)
    with self.assertRaisesRegex(EntityInstanceError, "ENTITY_DATA_MISSING"):
        self.runtime.read(self.entity_id)
```

- [ ] **Step 2: 运行 runtime RED**

Run: `cd backend; python -m unittest tests.test_entity_instance_l2_runtime -v`

Expected: FAIL，`ResolvedEntitySource` 只支持 `tag_id`。

- [ ] **Step 3: 扩展 source descriptor，不改变上层调用接口**

```python
@dataclass(frozen=True)
class ResolvedEntitySource:
    entity_instance_id: UUID
    definition_id: str
    instance_key: str
    device_instance_id: UUID
    source_kind: str
    source_id: UUID
    data_type: str
    unit: str | None
    direction: str
    freshness_seconds: float
    binding_id: UUID | None = None
    tag_id: UUID | None = None
    conversion_revision_id: UUID | None = None
    confirmation_audit_id: UUID | None = None
```

`source_kind` 只允许 `legacy_tag|point_conversion`。`EntityInstanceRegistry.resolve()` 对 legacy 继续校验 confirmation/tag catalog；对 point conversion 校验 active installed conversion、active output binding、entity/revision/site version 一致，不访问 tag catalog。

- [ ] **Step 4: 用一个 source-aware observation catalog 读取两种路径**

```python
class ObservationCatalog(Protocol):
    def latest(self, source: ResolvedEntitySource) -> SourceObservation | None:
        raise NotImplementedError

    def history(self, source: ResolvedEntitySource, range_key: str) -> list[SourceObservation]:
        raise NotImplementedError


class PostgresObservationCatalog:
    def latest(self, source: ResolvedEntitySource) -> SourceObservation | None:
        if source.source_kind == "legacy_tag":
            return self._latest_l0(source)
        if source.source_kind == "point_conversion":
            return self._latest_l2(source)
        raise EntityInstanceError("ENTITY_SOURCE_KIND_INVALID", "Entity source kind is invalid")
```

`SourceObservation` 增加 `event_id`、`reason`、`received_at`、`calculated_at`、`conversion_revision_id`、`site_configuration_version`、`source_digest`；`EntityInstanceObservation.public_dict()` 返回这些可读证据和 `source_kind`。BAD/STALE 在 `read()` 继续 409；`read_for_alarm()` 返回它们以断开持续确认；history 返回全部四态。

- [ ] **Step 5: 添加 L2 history HTTP seam**

```python
@router.get(
    "/entity-instances/{entity_instance_id}/history",
    **protected(RUNTIME_READ),
)
async def read_entity_instance_history(
    entity_instance_id: UUID,
    range: str = Query("1h", pattern="^(1h|6h|24h|7d)$"),
    runtime: EntityInstanceRuntime = Depends(get_entity_instance_runtime),
) -> dict:
    items = runtime.history(entity_instance_id, range)
    return {"items": [item.public_dict() for item in items], "total": len(items)}
```

- [ ] **Step 6: 验证告警、规则、策略和 EMS workbench 仍只学 Runtime**

测试通过 `EntityInstanceRuntime` 注入 point-conversion entity，分别调用 `EntityAlarmAdapter`、规则 context、EMS policy input 和 workbench metric；断言都获得同一 event/revision/source digest。静态 gate 锁定这些模块不直接查询 `t_telemetry_latest` 或 `t_tags`。

- [ ] **Step 7: 运行 runtime/public/upper-consumer 回归**

Run: `cd backend; python -m unittest tests.test_entity_instance_l2_runtime tests.test_entity_delivery_public_api tests.test_entity_alarm_adapter_contract tests.test_rule_alarm_adapter_contract tests.test_ems_policy_postgres -v`

Expected: L2 realtime/history、BAD/STALE、no-fallback、legacy unmigrated compatibility、upper-consumer source identity 全部 PASS。

- [ ] **Step 8: 提交 Task 7**

```bash
git add backend/app/services/entity_instance_registry.py backend/app/services/entity_instance_runtime.py backend/app/services/entity_instance_postgres.py backend/app/services/entity_instance_catalog.py backend/app/api/entity_instances.py backend/tests/test_entity_delivery_public_api.py backend/tests/test_entity_instance_l2_runtime.py
git commit -m "feat(data): serve entity observations from L2"
```

---

### Task 8: 分发提交后的 L2 outbox 并提供认证 entity WebSocket

**Files:**
- Create: `backend/app/services/data_trunk_outbox.py`
- Modify: `backend/app/api/websocket.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_entity_observation_websocket.py`
- Modify: `backend/tests/test_control_management_ws_security.py`

**Interfaces:**
- Consumes: `t_l2_stream_outbox`、既有一次性 WS ticket、`Identity.refresh()` session revalidation。
- Produces: `await OutboxDispatcher.run_once(limit: int = 200) -> int`；WS `/api/v1/ws/entity-observations`。

- [ ] **Step 1: 写 commit-only、重复投递和会话撤销 RED**

```python
def test_failed_ingest_never_reaches_entity_websocket(self):
    with self.authenticated_entity_socket() as socket:
        self.repository.fail_at = "outbox"
        self.protocol_publish(power=12345)
        self.assert_no_message(socket, timeout=0.2)

def test_committed_event_is_sent_with_stable_event_id_and_client_can_dedupe(self):
    with self.authenticated_entity_socket() as socket:
        self.protocol_publish(power=12345)
        first = socket.receive_json()
        self.force_outbox_redelivery(first["event_id"])
        second = socket.receive_json()
        self.assertEqual(first["event_id"], second["event_id"])

def test_logout_closes_existing_socket_before_next_subscription(self):
    with self.authenticated_entity_socket() as socket:
        self.logout_current_session()
        socket.send_json({"subscribe": [str(self.entity_id)]})
        self.assertEqual(socket.close_code, 4401)
```

- [ ] **Step 2: 运行 WebSocket RED**

Run: `cd backend; python -m unittest tests.test_entity_observation_websocket -v`

Expected: 404/close，entity-observations WS 尚不存在。

- [ ] **Step 3: 实现 outbox claim/publish/ack**

```python
class OutboxDispatcher:
    async def run_once(self, limit: int = 200) -> int:
        claimed = await asyncio.to_thread(self._repository.claim_unpublished, limit)
        published = 0
        for event in claimed:
            try:
                await self._broadcaster.publish(event)
            except Exception:
                await asyncio.to_thread(self._repository.record_attempt, event.event_id)
            else:
                await asyncio.to_thread(self._repository.mark_published, event.event_id)
                published += 1
        return published
```

Claim 使用单事务 CTE `FOR UPDATE SKIP LOCKED`，把未发布且 `next_attempt_at<=now()`、claim 已过期的行更新为当前 `worker_id` 和 `claimed_until=now()+30s` 后 `RETURNING`；不得在发送前标 published。成功按 `(event_id,worker_id)` 写 `published_at` 并清 claim；失败递增 attempts、按 `min(60, 2^attempts)` 秒设置 `next_attempt_at` 并清 claim。worker 崩溃后 lease 到期会重投，因此语义为 at-least-once。payload 只含 `event_id/entity_instance_id/definition_id/value/data_type/unit/quality/reason/observed_at/received_at/calculated_at/age_ms/conversion_revision_id/site_configuration_version/source_summary`。

- [ ] **Step 4: 实现认证 WS 协议**

客户端先发送既有一次性 ticket，再发送 `{"subscribe":["11111111-1111-1111-1111-111111111111"]}`。服务端每次 subscribe 调 `Identity.refresh(principal)` 和 `authorize(principal,"runtime.read")`，验证实体存在且不超过 500 项；响应 `{"type":"subscribed","entity_instance_ids":["11111111-1111-1111-1111-111111111111"]}`。增量消息 `type="entity_observation"`，token、L0 source path、topic 和完整来源图不进入 payload。

- [ ] **Step 5: 在 lifespan 启动 dispatcher/freshness loops**

`main.py` 只在 production adapters 成功构建后创建两个可取消 task：freshness interval 5 秒、outbox interval 250 ms；shutdown 先停止 intake，再 flush pipeline，再停止 freshness/outbox。任一构建失败在 production fail-fast，development 输出持续警告且不伪造数据。

- [ ] **Step 6: 运行 WS 与安全回归**

Run: `cd backend; python -m unittest tests.test_entity_observation_websocket tests.test_control_management_ws_security -v`

Expected: auth、ticket reuse、logout/revoke、commit-only、at-least-once event ID 全部 PASS。

- [ ] **Step 7: 提交 Task 8**

```bash
git add backend/app/services/data_trunk_outbox.py backend/app/api/websocket.py backend/app/main.py backend/tests/test_entity_observation_websocket.py backend/tests/test_control_management_ws_security.py
git commit -m "feat(data): stream committed L2 observations"
```

---

### Task 9: 建成引导式 PCS 数据主干交付驾驶舱

**Files:**
- Create: `frontend/src/components/data-trunk/DataTrunkWorkspace.tsx`
- Create: `frontend/src/components/data-trunk/NodeTrunkOverview.tsx`
- Create: `frontend/src/components/data-trunk/PointConversionPlanPanel.tsx`
- Create: `frontend/src/components/data-trunk/EntityObservationCard.tsx`
- Create: `frontend/src/components/data-trunk/dataTrunkRetryState.ts`
- Create: `frontend/src/components/data-trunk/dataTrunkRetryState.test.mjs`
- Create: `frontend/src/components/data-trunk/dataTrunkViewModel.ts`
- Create: `frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/pages/NodeTreePage.tsx`

**Interfaces:**
- Consumes: Task 6 REST、Task 7 L2 read/history、Task 8 entity WS、既有 `apiFetch` 和 auth session。
- Produces: 节点树内五阶段 PCS cockpit；actor/node/plan scoped apply retry；operator L2-only view。

- [ ] **Step 1: 写纯 view-model 和 retry-state RED**

```javascript
test("blockers keep apply disabled and expose one next action", () => {
  const model = buildDataTrunkViewModel({
    plan: {status: "blocked", items: [], blockers: [{code: "POINT_CONVERSION_INPUT_AMBIGUOUS", input_key: "operating_state_raw"}]},
  });
  assert.equal(model.canApply, false);
  assert.equal(model.nextAction, "请选择“运行状态”对应的原始点位");
});

test("retry key is restored only for the same actor node plan and digest", () => {
  saveApplyRetry(storage, {actorId: "engineer-1", nodeId: "node-1", planId: "plan-1", planDigest: "a".repeat(64), key: "key-1"});
  assert.equal(loadApplyRetry(storage, {actorId: "engineer-1", nodeId: "node-1", planId: "plan-1", planDigest: "a".repeat(64)}).key, "key-1");
  assert.equal(loadApplyRetry(storage, {actorId: "engineer-2", nodeId: "node-1", planId: "plan-1", planDigest: "a".repeat(64)}), null);
});
```

- [ ] **Step 2: 运行 Node RED**

Run: `cd frontend; node --test src/components/data-trunk/dataTrunkRetryState.test.mjs src/components/data-trunk/dataTrunkViewModel.test.mjs`

Expected: FAIL，模块不存在。

- [ ] **Step 3: 增加 typed client，不新增第二个 fetch wrapper**

```typescript
export type TrunkQuality = 0 | 1 | 64 | 192;
export type PlanAction = "add" | "update" | "preserve" | "delete_candidate" | "block";

export interface NodeDataTrunk {
  node: {id: string; name: string; device_category: string};
  compatibility_status: "legacy" | "point_conversion" | "not_installed";
  l0: Array<{display_name: string; source_key: string; data_type: string; unit: string | null; quality: TrunkQuality; observed_at: string | null}>;
  l1: {template_name: string; revision: number; installed_at: string; inputs_complete: boolean} | null;
  l2: EntityObservation[];
  match_rate: number;
  required_inputs_complete: boolean;
}

export interface EntityObservation {
  event_id: string;
  entity_instance_id: string;
  definition_id: string;
  display_name: string;
  value: number | boolean | string | string[] | null;
  data_type: "FLOAT" | "INT" | "BOOL" | "STRING" | "ENUM" | "CODE_SET";
  unit: string | null;
  quality: TrunkQuality;
  reason: string | null;
  observed_at: string;
  age_ms: number;
  conversion_revision_id: string;
  site_configuration_version: number;
  source_summary: string;
}

export interface CreatePointConversionPlan {
  template_revision_id: string;
  input_selections: Record<string, string>;
}

export interface PointConversionPlan {
  id: string;
  node_id: string;
  status: "ready" | "blocked" | "applied";
  items: Array<{item_key: string; action: PlanAction; display_name: string; before: unknown; after: unknown}>;
  blockers: Array<{code: string; input_key: string; message: string}>;
  digest: string;
}

export interface PointConversionApplication {
  id: string;
  plan_id: string;
  installed_conversion_id: string;
  solution_installation_id: string;
  site_configuration_version: number;
  output_entity_instance_ids: string[];
}

export async function fetchNodeDataTrunk(nodeId: string): Promise<NodeDataTrunk> {
  return apiFetch(`/nodes/${encodeURIComponent(nodeId)}/data-trunk`);
}

export async function createPointConversionPlan(nodeId: string, body: CreatePointConversionPlan): Promise<PointConversionPlan> {
  return apiFetch(`/nodes/${encodeURIComponent(nodeId)}/point-conversion-plans`, {method: "POST", body: JSON.stringify(body)});
}

export async function applyPointConversionPlan(planId: string, digest: string, key: string): Promise<PointConversionApplication> {
  return apiFetch(`/point-conversion-plans/${encodeURIComponent(planId)}/apply`, {method: "POST", headers: {"Idempotency-Key": key}, body: JSON.stringify({plan_digest: digest})});
}
```

Entity WebSocket 复用现有 ticket 获取流程，但连接 `/ws/entity-observations`；client 按 event ID 保留 bounded 1,000 项去重集合，断线重连后先 REST refresh 再订阅。

- [ ] **Step 4: 实现五阶段驾驶舱和角色投影**

`DataTrunkWorkspace` 固定显示：①节点树 ②连接与模板 ③匹配与预览 ④处理阻断 ⑤安装与验收。左侧仍由 `NodeTreePage` 控制物理节点；中部 `NodeTrunkOverview` 三列为 L0 原始点位、L1 点位转换、L2 全局实体；右侧只显示匹配率、必需输入完整度、blocker、下一动作、计划摘要和验收入口。

operator 只请求/显示 L2 cards 与 history，不请求 template/plan/configuration APIs；engineer/admin 可查看 L0/L1、生成 plan、消歧和 apply。`delete_candidate` 文案固定为“应用后停止生成新的 L2 观测；历史值与来源证据保留”。

- [ ] **Step 5: 实现 L2 实时/历史展示**

`EntityObservationCard` 显示中文质量：GOOD=正常、UNCERTAIN=存疑、BAD=无效、STALE=超时；BAD/STALE 当前值显示 `—`，另行标注最近正常值且明确“非当前值”。卡片显示单位、数据年龄、转换模板版本、站点配置版本和简短来源摘要；历史图按 observation quality 断线，不跨 BAD/STALE 插值。

- [ ] **Step 6: 实现结果未知和换牌语义**

apply key 在发请求前写入 sessionStorage；只有明确 201 或稳定业务 4xx 才清。网络中断、2xx body 截断、503 保留同 key。换牌计划区显示“输入绑定 A→B”“L2 实体 ID 保持”“告警/策略/画面引用保持”，不显示 UUID，只用 entity display name 和短版本号。

- [ ] **Step 7: 运行前端契约、类型和构建门禁**

Run: `cd frontend; node --test src/components/data-trunk/dataTrunkRetryState.test.mjs src/components/data-trunk/dataTrunkViewModel.test.mjs`

Run: `cd frontend; npx tsc -b`

Run: `cd frontend; npm run build`

Expected: 全部 exit 0；`rg -n "fetch\(" frontend/src` 只命中统一 wrapper 和 login；`rg -n "ws/telemetry" frontend/src/components/data-trunk frontend/src/pages/NodeTreePage.tsx` 零命中。

- [ ] **Step 8: 提交 Task 9**

```bash
git add frontend/src/components/data-trunk frontend/src/api/client.ts frontend/src/pages/NodeTreePage.tsx
git commit -m "feat(ui): guide PCS data trunk delivery"
```

---

### Task 10: 用公共协议/PostgreSQL/REST/WS/重启完成机器验收并收紧 PCS contract gate

**Files:**
- Create: `backend/app/services/data_trunk_acceptance.py`
- Create: `init-db/migration_039_pcs_data_trunk_contract_gate.sql`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/services/solution_delivery.py`
- Modify: `backend/app/services/solution_package_archive.py`
- Modify: `backend/tests/postgres_delivery_app.py`
- Create: `backend/tests/test_pcs_data_trunk_acceptance_postgres.py`
- Modify: `backend/tests/test_data_trunk_migration_postgres.py`
- Create: `reference-deliveries/pv-storage-charging-ems/acceptance/pcs-data-trunk.yaml`
- Modify: `reference-deliveries/pv-storage-charging-ems/package.yaml`
- Modify: `README.md`
- Modify: `CONTEXT.md`
- Modify: `docs/product-destination.md`
- Modify: `CODEX_HANDOFF.md`

**Interfaces:**
- Consumes: 既有 `SolutionDelivery.run_acceptance()` 与 `/solution-installations/{id}/acceptance-runs`、Task 4 公共协议模拟器、Task 7 REST、Task 8 WS。
- Produces: acceptance kind `data_trunk`，结果仍由既有 `/delivery-reports/{report_id}` 回读；PCS migrated contract gate。

- [ ] **Step 1: 写完整公共主缝 RED**

```python
class PcsDataTrunkAcceptancePostgresTest(PostgresPublicApiTestCase):
    def test_two_brand_pcs_data_trunk_is_atomic_restart_safe_and_identity_stable(self):
        engineer = self.login("engineer")
        operator = self.login("operator")
        installation = self.install_reference_package_with_brand_a(engineer)
        entity_ids_before = self.pcs_entity_ids(operator)

        with self.entity_socket(operator, entity_ids_before.values()) as socket:
            self.publish_neuron_brand_a(power_raw=12345, state="2", faults="E30;E11")
            self.assert_l0_raw(power=12345, unit="W")
            self.assert_l2_realtime("pcs.active_power", value=12.345, unit="kW", quality=192)
            self.assert_l2_realtime("pcs.operating_state", value="RUNNING", quality=192)
            self.assert_l2_realtime("pcs.fault_codes", value=["COMPRESSOR_FAULT", "DC_OVERVOLTAGE"], quality=192)
            self.assert_three_committed_ws_events(socket)

        self.prove_unknown_enum_bad_unknown_fault_uncertain_and_out_of_range_bad()
        self.prove_late_history_without_latest_or_ws_regression()
        self.prove_duplicate_message_and_injected_transaction_rollback()
        self.prove_stale_transition()

        brand_b = self.replace_with_brand_b(engineer)
        self.assertEqual(entity_ids_before, self.pcs_entity_ids(operator))
        self.publish_neuron_brand_b(power_kw=13.5, state="R", faults="C30,E11")
        self.assert_l2_realtime("pcs.active_power", value=13.5, unit="kW", quality=192)

        report = self.run_delivery_acceptance(engineer, brand_b["solution_installation_id"])
        self.assertEqual(self.acceptance_item(report, "acceptance.pcs-data-trunk")["status"], "passed")
        self.restart_app_process()
        self.assertEqual(self.get_report(report["id"]), report)
        self.assertEqual(self.pcs_entity_ids(operator), entity_ids_before)
        self.assertEqual(brand_b["site_configuration_version"], self.current_site_version())
```

- [ ] **Step 2: 运行主缝 RED**

Run: `$env:ZIZU_POSTGRES_TEST='1'; cd backend; python -m unittest tests.test_pcs_data_trunk_acceptance_postgres -v`

Expected: FAIL，acceptance whitelist 尚无 `data_trunk`。

- [ ] **Step 3: 添加 acceptance 资产和 validator**

```yaml
schemaVersion: zizu.acceptance/v1alpha1
id: acceptance.pcs-data-trunk
kind: data_trunk
required: true
deviceCategory: PCS
entityDefinitions:
  - pcs.active_power
  - pcs.operating_state
  - pcs.fault_codes
templateAssets:
  - pcs.brand-a
  - pcs.brand-b
checks:
  - l0_history_latest
  - numeric_enum_fault_conversion
  - quality_time_semantics
  - atomic_rollback_and_idempotency
  - committed_websocket
  - restart_persistence
  - brand_replacement_identity
```

Archive validator 只接受上列固定 checks，并验证引用的 template/entity assets 在同一包中。`SolutionDelivery` 的 acceptance kind whitelist 增加 `data_trunk`，不新增第二种报告模型或 endpoint。

`package.yaml.assets` 增加 `{id: acceptance.pcs-data-trunk, kind: acceptance, path: acceptance/pcs-data-trunk.yaml}`，根 `acceptance` 列表增加 `acceptance.pcs-data-trunk`；两处 ID 不一致或 required asset 未列入根列表时包导入 422。

- [ ] **Step 4: 实现 observer-only 验收分类器**

```python
class DataTrunkAcceptance:
    def evaluate(self, installation: InstallationOutcome, definition: Mapping[str, Any]) -> dict[str, Any]:
        evidence = self._repository.evidence_for_installation(installation.id, definition)
        return {
            "id": definition["id"],
            "kind": "data_trunk",
            "required": True,
            "status": "passed" if evidence.all_required else "failed",
            "code": evidence.machine_code,
            "evidence": evidence.public_summary(),
        }
```

验收只读取正常产品链产生的 L0/L2/source/outbox/plan/install/audit 证据，不发送遥测、不修改 plan、不生成 STALE、不创建实体。evidence 锁定 platform version、最高 schema 039、package digest、两个 template revision、site configuration version、三个 entity IDs、各 check 的 event/digest 摘要和时间；不包含 raw payload、topic、address、token、Secret。

- [ ] **Step 5: 添加 contract gate 负例**

```python
def test_migrated_pcs_cannot_create_direct_tag_binding(self):
    with self.assertRaises(psycopg2.errors.CheckViolation):
        self.insert_direct_binding_for_point_conversion_entity()

def test_migrated_upper_consumers_have_no_runtime_tag_fallback(self):
    forbidden = ("t_telemetry_latest", "t_tags", "legacy_tag")
    for module in (entity_alarm_adapter, rule_engine, ems_policy_runtime, ems_workbench):
        source = inspect.getsource(module)
        for token in forbidden:
            self.assertNotIn(token, source)
```

Migration 039 fresh/038-upgrade/replay 测试先 RED，再用下列 deferred constraint 语义 GREEN：

每个 `CREATE CONSTRAINT TRIGGER` 前先对同名 trigger 执行 `DROP TRIGGER IF EXISTS <name> ON <table>`，保证 owner migration replay 幂等。

```sql
CREATE OR REPLACE FUNCTION assert_entity_instance_single_source(target_id UUID)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  kind TEXT;
  active_tags INTEGER;
  active_outputs INTEGER;
BEGIN
  SELECT source_kind INTO kind FROM t_entity_instances WHERE id = target_id;
  SELECT count(*) INTO active_tags
    FROM t_entity_instance_bindings
    WHERE entity_instance_id = target_id AND active = TRUE;
  SELECT count(*) INTO active_outputs
    FROM t_conversion_output_bindings b
    JOIN t_installed_point_conversions i ON i.id = b.installed_conversion_id
    WHERE b.entity_instance_id = target_id AND i.current = TRUE;
  IF kind = 'point_conversion' AND (active_tags <> 0 OR active_outputs <> 1) THEN
    RAISE EXCEPTION 'point conversion entity must have exactly one conversion source and no direct tag source';
  END IF;
  IF kind = 'legacy_tag' AND active_outputs <> 0 THEN
    RAISE EXCEPTION 'legacy entity cannot have a point conversion source';
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION enforce_entity_instance_binding_source()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM assert_entity_instance_single_source(COALESCE(NEW.entity_instance_id, OLD.entity_instance_id));
  RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION enforce_entity_instance_kind_source()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM assert_entity_instance_single_source(COALESCE(NEW.id, OLD.id));
  RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION enforce_installed_conversion_sources()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_id UUID;
BEGIN
  FOR target_id IN
    SELECT DISTINCT entity_instance_id
    FROM t_conversion_output_bindings
    WHERE installed_conversion_id IN (OLD.id, NEW.id)
  LOOP
    PERFORM assert_entity_instance_single_source(target_id);
  END LOOP;
  RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_entity_instance_binding_single_source
AFTER INSERT OR UPDATE OR DELETE ON t_entity_instance_bindings
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION enforce_entity_instance_binding_source();

CREATE CONSTRAINT TRIGGER trg_conversion_output_binding_single_source
AFTER INSERT OR UPDATE OR DELETE ON t_conversion_output_bindings
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION enforce_entity_instance_binding_source();

CREATE CONSTRAINT TRIGGER trg_entity_instance_kind_single_source
AFTER INSERT OR UPDATE OF source_kind ON t_entity_instances
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION enforce_entity_instance_kind_source();

CREATE CONSTRAINT TRIGGER trg_installed_conversion_single_source
AFTER INSERT OR UPDATE OF current OR DELETE ON t_installed_point_conversions
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION enforce_installed_conversion_sources();
```

在 `t_entity_instance_bindings`、`t_conversion_output_bindings` 上创建调用 binding wrapper 的 `DEFERRABLE INITIALLY DEFERRED` constraint trigger；在 `t_entity_instances` 上创建 kind wrapper；在 `t_installed_point_conversions` 上创建 installed wrapper。`verify_data_trunk_contract_gate()` 查询双来源、point-conversion 零/多 current output 和 legacy conversion output 三类违规，production lifespan 在启动 ingestion 前 fail-fast。未迁移 legacy entity 继续允许一个 confirmed direct tag。Migration 039 不修改 038 已登记内容。

- [ ] **Step 6: 运行完整机器验收和权限负例**

Run: `$env:ZIZU_POSTGRES_TEST='1'; cd backend; python -m unittest tests.test_pcs_data_trunk_acceptance_postgres tests.test_point_conversion_postgres tests.test_data_trunk_postgres tests.test_data_trunk_migration_postgres -v`

Expected: 公共协议→parser→DataTrunk→真实 PG→REST/WS→重启→换牌→report 全部 PASS；operator plan/apply 403 且零写/有拒绝审计。

- [ ] **Step 7: 同步文档但不覆盖既有未提交内容**

对 `README.md` 只补公开 YAML、REST/WS、质量和运行命令；`CONTEXT.md` 只补最终出现的机器码/术语；`docs/product-destination.md` 将 PCS 主缝标记为“已通过机器验收”时必须引用实际测试证据，不得宣称 BMS/PV/EVSE/电表或现场一小时/四小时试验完成。`CODEX_HANDOFF.md` 记录每条命令、结果、commit 和未部署边界。

- [ ] **Step 8: 运行最终全量门禁**

Run: `cd backend; python -m unittest discover -s tests -p "test_*.py"`

Run: `$env:ZIZU_POSTGRES_TEST='1'; cd backend; python -m unittest tests.test_pcs_data_trunk_acceptance_postgres tests.test_data_trunk_migration_postgres tests.test_data_trunk_postgres tests.test_point_conversion_postgres -v`

Run: `cd backend; python -m compileall app tests`

Run: `cd frontend; node --test src/components/data-trunk/dataTrunkRetryState.test.mjs src/components/data-trunk/dataTrunkViewModel.test.mjs`

Run: `cd frontend; npx tsc -b`

Run: `cd frontend; npm run build`

Run: `git diff --check`

Expected: 全部 exit 0；完整 backend suite 无新增失败；PostgreSQL 主缝无 skip；前端构建无类型错误。

- [ ] **Step 9: 提交 Task 10**

```bash
git add backend/app/services/data_trunk_acceptance.py init-db/migration_039_pcs_data_trunk_contract_gate.sql backend/app/services/data_trunk_postgres.py backend/app/main.py backend/app/services/solution_delivery.py backend/app/services/solution_package_archive.py backend/tests/postgres_delivery_app.py backend/tests/test_pcs_data_trunk_acceptance_postgres.py backend/tests/test_data_trunk_migration_postgres.py reference-deliveries/pv-storage-charging-ems/acceptance/pcs-data-trunk.yaml reference-deliveries/pv-storage-charging-ems/package.yaml
git add -p README.md CONTEXT.md docs/product-destination.md CODEX_HANDOFF.md
git diff --cached --check
git commit -m "feat(data): prove PCS data trunk delivery"
```

---

## Plan Author Self-Review — Completed 2026-08-17

| 正式规格 | 实施任务 |
|---|---|
| §2—4 PCS 三项语义、L0/L1/L2 不变量 | Task 1、3、5、7 |
| §5 深模块唯一写接口 | Task 1、2、4、8 |
| §6 同事务数据流与 receipt 后确认 | Task 2、4 |
| §7 四态质量、乱序、新鲜度、重试失败 | Task 3、4 |
| §8 强关系与 L0/L2 时序 | Task 2、10 |
| §9 确定性计划与原子安装 | Task 5、6 |
| §10 REST/WS/角色/审计 | Task 6、8 |
| §11 引导式交付驾驶舱 | Task 9 |
| §12 稳定错误与 fail closed | Task 5、6、7、10 |
| §13 公共协议、PG、REST、WS、重启、两品牌机器验收 | Task 10 |
| §14 expand/migrate/contract | Task 2、5—7、10 |
| §15 先数值原子主缝，再质量/计划/换牌/UI | Task 1→10 的执行顺序 |

- 占位符扫描已完成：无未定项标记、省略实现步骤或空测试体；连续三点只存在于 Python variadic tuple 类型和 SQL conflict-update 语法说明。
- 类型一致性已完成：`TrunkQuality=0/1/64/192`、机器 entity IDs 为 snake_case、installation plan 使用复数 `point_conversion_plans`、application 同时返回 installed conversion 和 solution installation lineage、品牌 B 验收使用 derived solution installation。
- migration 一致性已完成：038 只做 expand 且登记后不改写；039 单独加入 migrated PCS contract gate；fresh、037/038 upgrade 和 replay 都有真实 PostgreSQL 门禁。
- 当前四个已有文档改动不属于本计划文件提交；执行 Task 10 时必须 `git add -p`，不得覆盖或顺手提交无关 hunk。

## Implementation Final Review Gates

- [ ] **Spec coverage:** 将正式规格第 2—15 节逐条映射到 Task 1—10；重点确认同事务、四态质量、两品牌稳定身份、公开协议/REST/WS/重启报告和 contract gate 均有执行测试。
- [ ] **Deep-module review:** 外部业务写 seam 仍只有 `DataTrunk.ingest()`；freshness 和 outbox 属内部协作者；pipeline、HTTP、WS 和 acceptance 不复制转换规则。
- [ ] **Schema review:** 关系关键字段无 JSON 替代；所有 typed-value、quality、revision、binding、plan、source、idempotency、append-only 不变量有数据库约束或事务测试。
- [ ] **Safety review:** BAD/STALE 不带当前值；late 不倒退 latest；失败不丢 buffer；migrated PCS 不 fallback；控制仍走独立安全 action，不从 L2 反推 L0。
- [ ] **Public-contract review:** 请求/响应字段、机器码、capability、OpenAPI 路由分类、README 示例和 reference package 完全一致。
- [ ] **UI review:** 实施工程师无需 UUID/SQL/JSON/YAML；operator 只看 L2；blocker 清零前安装禁用；窄屏单列；未知结果使用同 actor/node/plan/key 重放。
- [ ] **Release wording:** 只声明“PCS 的 L0—L2 数据主干纵向切片通过机器验收，并证明两品牌替换不改变上层业务身份”。
