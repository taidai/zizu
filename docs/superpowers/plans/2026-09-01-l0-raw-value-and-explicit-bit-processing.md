# L0 Raw Value and Explicit BIT Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Neuron `BIT` 点位在 L0 中恢复为设备实际上传的整数 `0/1`，异常原值保留并标 BAD；把“直接使用”改为真正的强类型透传，并通过显式的“0/1 转布尔”L1 加工生成 BOOL L2，同时保留 L2 上次正常值与当前异常状态，最终发布 v0.6.8 并在 1 号机沿主干验收。

**Architecture:** 不增加表族、运行链或规则引擎。现有单写者数据帧仍是唯一运行入口：Neuron/MQTT 原始标量进入 L0 typed union，经现有 L1 DAG 运行时执行 `passthrough` 或编译后的 `boolean_map`，事务 B 原子提交 L2 与 outbox。Schema 059 只扩充原始质量原因、L2 上次正常值时间和两种强类型规则的配置持久化；旧 BIT→BOOL 配置通过维护窗口中的确定性修订迁移硬切，不提供双写或兼容 fallback。

**Tech Stack:** FastAPI/Python 3、PostgreSQL/TimescaleDB、NanoMQ、Neuron、React/TypeScript、Node Test、Playwright、Docker Compose、GitHub Actions 多架构镜像。

**Spec:** `docs/superpowers/specs/2026-09-01-l0-raw-value-and-explicit-bit-processing-design.md`

## Global Constraints

- 唯一主干保持为：真实节点树 → L0 原始点位 → L1 点位加工 → L2 全局实体 → 告警/JDM/控制/固定 EMS 工作台。
- L0 中数字 `0`、字符串 `"0"`、布尔 `false` 必须是三个不同的 typed value；读取不得根据点位预期类型改写事实。
- `wire_data_type=BIT` 的正常 L0 值为 `ValueKind.INT` 的 `0/1`。异常值仍入库，但质量为 BAD 且原因明确。
- “直接使用”不得调用公式编译器；“0/1 转布尔”可编译为现有安全公式 AST，但对用户和配置导出保持独立的 `boolean_map` 规则种类。
- 上层应用继续只消费 committed L2。非 GOOD 输入不得进入告警值判断、JDM、控制或设备写。
- 不新增 Redis、Kafka、微服务、时序表、第二套规则引擎、运行期 fallback 或兼容 API。
- 每个任务只提交本任务文件；先检查工作树，保留用户已有改动，不提交无关文件。
- 任何“完成”结论必须有新鲜测试输出。部署验收不得执行控制、自动策略或设备写。

## File and Responsibility Map

- `backend/app/services/data_trunk.py`：MQTT 解析值到 L0 typed value 的唯一适配边界。
- `backend/app/services/data_trunk_contracts.py`：L0/L1/L2 不可变领域契约。
- `backend/app/services/data_trunk_postgres.py`：帧事务 A/B、L0/L2 history/latest 持久化与恢复。
- `backend/app/services/data_trunk_outbox.py`、`backend/app/services/committed_frame_stream_postgres.py`：提交后 L0/L2 投影和机器消费者快照。
- `backend/app/services/neuron_point_processing_catalog.py`、`backend/app/services/neuron_tag_import.py`：Neuron 协议类型与 L0 预期标量目录。
- `backend/app/services/point_processing_templates.py`、`backend/app/services/point_processing_postgres.py`：L1 规则验证、版本化持久化和重建。
- `backend/app/services/data_trunk_conversion.py`：L1 DAG 中的唯一运行求值入口。
- `backend/app/services/l0_raw_cutover.py`、`scripts/prepare_l0_raw_hard_cut.py`：Schema 059 上线前预检、确定性配置迁移与运行数据清理。
- `frontend/src/components/NodeTagPanel.tsx`：L0 实时/历史与点位加工入口。
- `frontend/src/components/data-trunk/InlinePointProcessingPanel.tsx`、`inlinePointProcessingModel.ts`：直接使用、0/1 转布尔和预览交互。
- `frontend/src/components/data-trunk/EntityObservationCard.tsx`、`dataTrunkViewModel.ts`：L2 当前状态、上次正常值和来源展示。
- `frontend/e2e/node-management.spec.ts`：发布前主干业务验收。
- `init-db/migration_059_l0_raw_bit_semantics.sql`：本次唯一 Schema 变更。

---

### Task 1: 固化 L0 原值解码与 BIT 目录契约

**Files:**

- Modify: `backend/app/services/data_trunk_contracts.py`
- Modify: `backend/app/services/data_trunk.py`
- Modify: `backend/app/services/neuron_point_processing_catalog.py`
- Modify: `backend/app/services/pipeline.py`
- Test: `backend/tests/test_data_trunk_raw_adapter.py`
- Test: `backend/tests/test_neuron_point_processing_catalog.py`
- Test: `backend/tests/test_neuron_tag_import.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class RawObservation:
    # existing fields remain unchanged
    quality_reason: str | None = None

@dataclass(frozen=True)
class TagMetadata:
    # existing fields remain unchanged
    wire_data_type: str | None

def decode_raw_scalar(raw_value: object) -> TypedValue: ...
def validate_raw_value(
    value: TypedValue,
    *,
    wire_data_type: str | None,
    expected_data_type: str,
) -> tuple[TrunkQuality, str | None]: ...
```

`decode_raw_scalar` 只按 Python/JSON 实际标量类型生成 FLOAT/INT/BOOL/STRING；特别注意 `bool` 必须先于 `int` 判断。`validate_raw_value` 只判质量，不改值：BIT+INT 0/1 为 GOOD，BIT+INT 其他值为 BAD/`BIT_VALUE_OUT_OF_RANGE`，BIT+非 INT 为 BAD/`TYPE_MISMATCH`。Neuron 类型映射固定为 `_TYPE_MAP[11] == ("BIT", "INT")`。

- [ ] 在 `test_data_trunk_raw_adapter.py` 把旧“0→false、1→true、2 被丢弃”断言改为失败测试：0/1 都是 INT；2、`"0"` 和 `False` 都保留原值但为 BAD，并检查精确原因。
- [ ] 增加来源摘要回归：同一 tag/time 下 INT 0 与 BOOL false 的 `source_digest` 不同。
- [ ] 运行 `python -m unittest tests.test_data_trunk_raw_adapter -v`，确认失败来自现有 `_raw_typed_value` 的 BOOL 规范化或丢弃行为。
- [ ] 在目录测试中新增 BIT 映射断言，在导入测试中断言 `wire_data_type=BIT`、`value_data_type=INT`；运行对应两个测试模块并确认旧映射导致失败。
- [ ] 在 `data_trunk_contracts.py` 增加可选 `quality_reason`，保证现有调用不必一次性传值；在 `data_trunk.py` 用 `decode_raw_scalar` 和 `validate_raw_value` 替换按配置类型转换及 `None` 丢弃分支。
- [ ] 在 `TagMetadata` 和 `pipeline.py` 两个构造位置传入 `wire_data_type`；修改 Neuron BIT 目录映射为 INT，不改变其他协议类型。
- [ ] 运行：

```powershell
cd backend
python -m unittest tests.test_data_trunk_raw_adapter tests.test_neuron_point_processing_catalog tests.test_neuron_tag_import -v
```

- [ ] 确认所有新增断言通过，且普通 FLOAT/INT/BOOL/STRING 既有测试不回归。
- [ ] 提交：`git add backend/app/services/data_trunk_contracts.py backend/app/services/data_trunk.py backend/app/services/neuron_point_processing_catalog.py backend/app/services/pipeline.py backend/tests/test_data_trunk_raw_adapter.py backend/tests/test_neuron_point_processing_catalog.py backend/tests/test_neuron_tag_import.py && git commit -m "fix(l0): preserve raw BIT scalar values"`

---

### Task 2: 用 Schema 059 保真持久化 L0 值、质量原因和恢复结果

**Files:**

- Create: `init-db/migration_059_l0_raw_bit_semantics.sql`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/app/services/data_trunk_outbox.py`
- Modify: `backend/app/services/committed_frame_stream_postgres.py`
- Modify: `backend/app/services/point_processing_postgres.py`
- Modify: `backend/app/api/tags.py`
- Test: `backend/tests/test_data_frames_postgres.py`
- Test: `backend/tests/test_committed_frame_stream_postgres.py`
- Test: `backend/tests/test_point_processing_postgres.py`
- Test: `backend/tests/test_data_trunk_migration_postgres.py`

**Interfaces:**

Schema 059 添加：

```sql
ALTER TABLE t_telemetry ADD COLUMN quality_reason TEXT;
ALTER TABLE t_telemetry_latest ADD COLUMN quality_reason TEXT;
```

迁移必须幂等、持有 `zizu-schema-059` advisory lock，并把两张表的 typed-union CHECK 固定为“恰好一个 raw_value_* 非空”，不依据 `t_tags.value_data_type`。历史恢复、latest、outbox、L1 预览和 API 必须通过实际非空字段调用同一个 `_typed_raw_value(...)` 重建 `TypedValue`。

```python
def _typed_raw_value(
    *,
    raw_value_float: float | None,
    raw_value_int: int | None,
    raw_value_bool: bool | None,
    raw_value_text: str | None,
) -> TypedValue: ...

@dataclass(frozen=True)
class CommittedL0Change:
    # existing fields remain unchanged
    quality_reason: str | None
```

- [ ] 在真实 PostgreSQL 测试中新增一组 round-trip：BIT 的 INT 0/1 进入 `raw_value_int`，`raw_value_bool IS NULL`；异常 INT 2、字符串和 BOOL 分别保留到对应列并带 BAD reason。
- [ ] 新增 latest、history、frame recovery、committed snapshot、outbox 和 L1 preview 的断言，确保都按实际非空列恢复；加入 INT 0 与 BOOL false 同时存在时不串型的案例。
- [ ] 增加 migration 059 结构测试：两张 L0 表存在 `quality_reason`，约束允许任意一种实际标量字段但拒绝零个或多个字段。
- [ ] 运行：

```powershell
cd backend
python -m unittest tests.test_data_frames_postgres tests.test_committed_frame_stream_postgres tests.test_point_processing_postgres tests.test_data_trunk_migration_postgres -v
```

确认新增测试因 reason 未持久化、读取依赖预期类型或 Schema 059 不存在而失败。
- [ ] 编写迁移 SQL；不得新增 L0 表、规范值列或双值列。
- [ ] 在 `data_trunk_postgres.py` 集中实现 `_typed_raw_value`，写入历史/latest 时同时保存 `quality_reason`，所有 recovery/read path 删除“按 tag.value_data_type 选列”的逻辑。
- [ ] 在 outbox、committed stream、point-processing preview 和 tags API 透传 `quality_reason`，对 STALE 等运行期原因保持既有优先级：当前有效质量产生的原因优先，原始 BAD reason 次之。
- [ ] 重跑上述四个模块；再运行 `python -m unittest tests.test_data_frame_outbox tests.test_delivery_postgres_public_api -v`，确认 API 与 outbox 契约无回归。
- [ ] 提交：`git add init-db/migration_059_l0_raw_bit_semantics.sql backend/app/services/data_trunk_postgres.py backend/app/services/data_trunk_outbox.py backend/app/services/committed_frame_stream_postgres.py backend/app/services/point_processing_postgres.py backend/app/api/tags.py backend/tests/test_data_frames_postgres.py backend/tests/test_committed_frame_stream_postgres.py backend/tests/test_point_processing_postgres.py backend/tests/test_data_trunk_migration_postgres.py && git commit -m "feat(l0): persist raw scalar quality evidence"`

---

### Task 3: 把“直接使用”改成真正的 passthrough

**Files:**

- Modify: `backend/app/services/data_trunk_contracts.py`
- Modify: `backend/app/services/data_trunk_conversion.py`
- Modify: `backend/app/services/point_processing_templates.py`
- Modify: `backend/app/services/point_processing_postgres.py`
- Modify: `init-db/migration_059_l0_raw_bit_semantics.sql`
- Test: `backend/tests/test_data_trunk_conversion.py`
- Test: `backend/tests/test_point_processing_templates.py`
- Test: `backend/tests/test_point_processing_postgres.py`
- Test: `backend/tests/test_point_processing_public_api.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class PassthroughTransform:
    input: InputReference

# canonical template JSON
{"kind": "passthrough", "input": "source_input_id"}
```

Schema 059 增加配置表：

```sql
CREATE TABLE t_point_processing_passthrough_rules (
  output_id UUID PRIMARY KEY REFERENCES t_point_processing_outputs(id) ON DELETE CASCADE,
  input_id UUID NOT NULL REFERENCES t_point_processing_inputs(id)
);
```

验证规则：单输入、required、cardinality=one、输入输出 `dataType` 完全相同、单位（含 NULL）完全相同。运行时若输入非 GOOD，沿用输入质量/原因并不产生新值；GOOD 时原样返回同一个 `TypedValue`，不触发公式编译。

- [ ] 在模板测试中新增合法 INT/BOOL/STRING passthrough，以及类型不同、单位不同、零输入、多输入的精确拒绝案例。
- [ ] 使用 mock/patch 增加关键回归：规划合法 passthrough 时 `compile_formula` 调用次数必须为 0，彻底关闭 `Formula syntax is invalid` 路径。
- [ ] 在转换测试中覆盖 GOOD 原样值和 BAD/STALE 质量传播。
- [ ] 在 PostgreSQL/API 测试中覆盖 draft plan → immutable revision persist → reload，导出的 canonical JSON 仍为 `kind=passthrough`。
- [ ] 运行四个测试模块，确认现有 numeric/formula 伪装行为导致 RED。
- [ ] 增加 `PassthroughTransform`，在 `evaluate_processing` / `_evaluate_output` 中直接取唯一输入；禁止调用公式 evaluator。
- [ ] 扩展 `_OUTPUT_TYPES`、`_parse_transform` 和 Postgres persist/load；把配置表 SQL 合并到 migration 059，避免第二个 schema 号。
- [ ] 重跑四个测试模块并确认全绿。
- [ ] 提交：`git add backend/app/services/data_trunk_contracts.py backend/app/services/data_trunk_conversion.py backend/app/services/point_processing_templates.py backend/app/services/point_processing_postgres.py init-db/migration_059_l0_raw_bit_semantics.sql backend/tests/test_data_trunk_conversion.py backend/tests/test_point_processing_templates.py backend/tests/test_point_processing_postgres.py backend/tests/test_point_processing_public_api.py && git commit -m "feat(l1): add strong typed passthrough"`

---

### Task 4: 增加显式 `boolean_map` 点位加工

**Files:**

- Modify: `backend/app/services/data_trunk_contracts.py`
- Modify: `backend/app/services/data_trunk_conversion.py`
- Modify: `backend/app/services/point_processing_templates.py`
- Modify: `backend/app/services/point_processing_postgres.py`
- Modify: `init-db/migration_059_l0_raw_bit_semantics.sql`
- Test: `backend/tests/test_data_trunk_conversion.py`
- Test: `backend/tests/test_point_processing_templates.py`
- Test: `backend/tests/test_point_processing_postgres.py`
- Test: `backend/tests/test_point_processing_public_api.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class BooleanMapTransform:
    input: InputReference
    true_when: int  # exactly 0 or 1
    compiled: CompiledFormula

# canonical template JSON
{"kind": "boolean_map", "input": "source_input_id", "trueWhen": 1}
```

Schema 059 增加：

```sql
CREATE TABLE t_point_processing_boolean_map_rules (
  output_id UUID PRIMARY KEY REFERENCES t_point_processing_outputs(id) ON DELETE CASCADE,
  input_id UUID NOT NULL REFERENCES t_point_processing_inputs(id),
  true_when SMALLINT NOT NULL CHECK (true_when IN (0, 1)),
  compiled_ast JSONB NOT NULL,
  ast_digest CHAR(64) NOT NULL
);
```

只接受单个 required/one/INT 输入和 BOOL 输出；单位必须为 NULL。平台把规则编译为 `input == 1` 或 `input == 0`，继续用现有安全 AST 求值器，不开放任意表达式。

- [ ] 在模板/API 测试中覆盖 `trueWhen=1` 与 `trueWhen=0`；拒绝 bool 类型的 `trueWhen`、其他整数、非 INT 输入、非 BOOL 输出、单位非空和多输入。
- [ ] 在转换测试中断言 0/1 的四种映射结果；INT 2 即使被直接构造为 GOOD，也必须失败关闭为 BAD/`BIT_VALUE_OUT_OF_RANGE`，防止绕过入口验证。
- [ ] 在 Postgres 测试中断言 immutable revision 能持久化/reload 高层 `boolean_map`，AST digest 稳定且运行仍走现有 compiled formula evaluator。
- [ ] 运行四个测试模块，确认 `boolean_map` 未识别而 RED。
- [ ] 增加 `BooleanMapTransform`、模板编译/校验、Postgres persist/load 和运行求值；保持 `FormulaTransform` 作为高级公式，不复制 evaluator。
- [ ] 重跑四个测试模块，确认两种 trueWhen、BAD 输入和重启重建都通过。
- [ ] 提交：`git add backend/app/services/data_trunk_contracts.py backend/app/services/data_trunk_conversion.py backend/app/services/point_processing_templates.py backend/app/services/point_processing_postgres.py init-db/migration_059_l0_raw_bit_semantics.sql backend/tests/test_data_trunk_conversion.py backend/tests/test_point_processing_templates.py backend/tests/test_point_processing_postgres.py backend/tests/test_point_processing_public_api.py && git commit -m "feat(l1): add explicit zero one boolean mapping"`

---

### Task 5: 区分 L2 上次正常值与当前 BAD 状态

**Files:**

- Modify: `init-db/migration_059_l0_raw_bit_semantics.sql`
- Modify: `backend/app/services/data_trunk_contracts.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/app/services/committed_frame_stream_postgres.py`
- Modify: `backend/app/services/data_trunk_outbox.py`
- Modify: `backend/app/api/entity_instances.py`
- Test: `backend/tests/test_data_frames_postgres.py`
- Test: `backend/tests/test_committed_frame_stream_postgres.py`
- Test: `backend/tests/test_delivery_postgres_public_api.py`
- Test: `backend/tests/test_alarm_runtime.py`
- Test: `backend/tests/test_committed_l2_jdm_consumer.py`
- Test: `backend/tests/test_control_command_runtime.py`

**Interfaces:**

Schema 059 增加：

```sql
ALTER TABLE t_l2_latest ADD COLUMN value_observed_at TIMESTAMPTZ;
UPDATE t_l2_latest SET value_observed_at=observed_at
WHERE quality=192 AND value_observed_at IS NULL;
```

API/latest 投影增加 `value_observed_at`，而现有 `observed_at` 继续表示当前质量事件时间。`t_l2_observations` 仍记录每次历史事实：GOOD 行有新值，BAD 行可无业务值但包含当前时间/原因/来源。

`t_l2_latest` upsert 规则：

- GOOD 且有 typed value：替换 typed value，`value_observed_at=EXCLUDED.observed_at`。
- BAD/UNCERTAIN/STALE 或无值：保留旧 typed value 与旧 `value_observed_at`，但更新当前 `observed_at`、quality、reason、来源、配置修订和 frame sequence。
- 首次即 BAD：typed value 与 `value_observed_at` 均为空。
- Schema 059 同步替换 `chk_l2_latest_typed_value` 与 `validate_l2_typed_value_against_entity()`：BAD/STALE latest 只有在 `value_observed_at IS NOT NULL` 时才允许保留一个与实体类型匹配的 typed value；历史 `t_l2_observations` 的 BAD 行仍保持无值。数据库约束不能把 retained value 误判为当前 GOOD。

- [ ] 在 PostgreSQL 测试中先提交 GOOD false，再提交 BAD；断言 latest 为“value=false、value_observed_at=GOOD 时间、quality=BAD、observed_at=BAD 时间、reason/source=本次异常”。
- [ ] 覆盖首次 BAD 不伪造值，以及随后新 GOOD 恢复并推进 value time。
- [ ] 在 committed snapshot/API 测试中断言两种时间都返回，机器状态仍是 BAD。
- [ ] 在 `test_alarm_runtime.py`、`test_committed_l2_jdm_consumer.py` 和 `test_control_command_runtime.py` 中断言 retained value 不得参与条件判断；BAD/STALE 一律 fail closed。
- [ ] 运行上述模块并确认旧 latest 全量覆盖值或 API 缺字段导致 RED。
- [ ] 修改 Schema、upsert CASE 表达式、领域投影、stream/outbox 和 API serializer；不得把 retained value 的质量改回 GOOD。
- [ ] 重跑测试；额外运行 `python -m unittest tests.test_control_command_runtime -v`，确认控制继续拒绝非 GOOD。
- [ ] 提交：`git add init-db/migration_059_l0_raw_bit_semantics.sql backend/app/services/data_trunk_contracts.py backend/app/services/data_trunk_postgres.py backend/app/services/committed_frame_stream_postgres.py backend/app/services/data_trunk_outbox.py backend/app/api/entity_instances.py backend/tests/test_data_frames_postgres.py backend/tests/test_committed_frame_stream_postgres.py backend/tests/test_delivery_postgres_public_api.py backend/tests/test_alarm_runtime.py backend/tests/test_committed_l2_jdm_consumer.py backend/tests/test_control_command_runtime.py && git commit -m "feat(l2): retain last good value on bad input"`

---

### Task 6: 建立确定性硬切预检、配置修订迁移和运行数据清理

**Files:**

- Create: `backend/app/services/l0_raw_cutover.py`
- Create: `scripts/prepare_l0_raw_hard_cut.py`
- Create: `scripts/test_prepare_l0_raw_hard_cut.py`
- Modify: `backend/tests/test_data_trunk_migration_postgres.py`
- Modify: `init-db/migration_059_l0_raw_bit_semantics.sql`

**Interfaces:**

```python
@dataclass(frozen=True)
class CutoverBlocker:
    node_id: UUID
    processing_revision_id: UUID
    output_id: UUID
    code: str

@dataclass(frozen=True)
class CutoverReport:
    deterministic_output_ids: tuple[UUID, ...]
    blockers: tuple[CutoverBlocker, ...]
    digest: str

def inspect_cutover(connection) -> CutoverReport: ...
def apply_cutover(
    connection, *, expected_digest: str, actor: str
) -> tuple[UUID, ...]: ...
def clear_runtime_test_data(
    connection, *, expected_configuration_revision: int
) -> dict[str, int]: ...
```

CLI 只提供三种互斥模式：

```text
python scripts/prepare_l0_raw_hard_cut.py --inspect
python scripts/prepare_l0_raw_hard_cut.py --apply --expected-digest DIGEST --actor release-v0.6.8
python scripts/prepare_l0_raw_hard_cut.py --clear-runtime --expected-config-revision REVISION
```

数据库凭据只能沿现有环境变量/连接配置读取，禁止作为 CLI 参数输出。`--apply` 和 `--clear-runtime` 必须检查写者 lease/后台进程已停止。任何 blocker、digest 变化或配置修订变化都整笔拒绝。

- [ ] 用 fake repository/connection 为 CLI 参数、JSON 报告、非零退出码写脚本单测：有 blocker、digest mismatch、writer active、revision mismatch 均不得写。
- [ ] 在真实 PostgreSQL 测试中建立三类配置：精确旧 BIT→BOOL identity、复杂 BOOL formula、无关非 BIT。断言预检只把第一类列为 deterministic、第二类列为 blocker。
- [ ] 增加 apply 测试：为确定性项创建新的 immutable processing revision，使用 `boolean_map trueWhen=1`，保留 entity instance ID、definition key、名称和上层引用；旧修订仅留审计。
- [ ] 增加 clear 测试：清空 L0/L2 observations/latest、alarm events、data frames、outbox、dedup/runtime cursors；节点、tags、templates/revisions、entity definitions/instances、alarm/JDM/control 配置不变。
- [ ] 先运行 `python -m unittest scripts.test_prepare_l0_raw_hard_cut backend.tests.test_data_trunk_migration_postgres -v`，确认模块不存在而 RED。
- [ ] 实现 inspect/apply/clear；digest 必须由排序后的 canonical JSON 计算，确保多次 inspect 稳定。apply/clear 各自单事务，异常回滚。
- [ ] 在 Schema 059 中只放通用 schema/backfill，不在 SQL 中猜测复杂公式；业务配置迁移由上述工具完成。
- [ ] 重跑专项，确认 blocked 场景数据库行数完全不变，确定性场景可重启后恢复新活动修订。
- [ ] 提交：`git add backend/app/services/l0_raw_cutover.py scripts/prepare_l0_raw_hard_cut.py scripts/test_prepare_l0_raw_hard_cut.py backend/tests/test_data_trunk_migration_postgres.py init-db/migration_059_l0_raw_bit_semantics.sql && git commit -m "feat(migration): add L0 raw hard cut tooling"`

---

### Task 7: 完成 L0、点位加工和 L2 的用户界面闭环

**Files:**

- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/components/NodeTagPanel.tsx`
- Modify: `frontend/src/components/data-trunk/inlinePointProcessingModel.ts`
- Modify: `frontend/src/components/data-trunk/InlinePointProcessingPanel.tsx`
- Modify: `frontend/src/components/data-trunk/dataTrunkViewModel.ts`
- Modify: `frontend/src/components/data-trunk/EntityObservationCard.tsx`
- Modify: `frontend/src/components/data-trunk/inlinePointProcessingModel.test.mjs`
- Modify: `frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs`
- Modify: `frontend/src/components/rawPointHistoryModel.test.mjs`

**Interfaces:**

```ts
export type InlinePointProcessingMode =
  | 'passthrough'
  | 'boolean_map'
  | 'numeric'
  | 'state'
  | 'formula'

export interface InlineRawPoint {
  id: string
  name: string
  display_name: string
  wire_data_type: string | null
  data_type: 'FLOAT' | 'INT' | 'BOOL' | 'STRING'
  unit: string | null
}

export interface L2LatestValue {
  value: unknown
  quality: string
  reason: string | null
  observed_at: string
  value_observed_at: string | null
}
```

前端生成的直接使用 JSON 必须是 `{kind:'passthrough',input}`；`boolean_map` 为 `{kind:'boolean_map',input,trueWhen:0|1}`。仅高级公式显示公式输入框。

- [ ] 更新 model tests：直接使用 FLOAT/INT/BOOL 都生成 passthrough；任意类型或单位差异（包括 NULL→`kW`）在提交前阻断，且不产生 expression。
- [ ] 新增 boolean_map tests：只对单个 BIT/INT 点位可选，结果类型锁定 BOOL，默认 `trueWhen=1`，两种选择生成精确 canonical JSON。
- [ ] 更新 view model tests：`BIT_VALUE_OUT_OF_RANGE` 显示“设备返回的 BIT 值不是 0 或 1”，`TYPE_MISMATCH` 显示“设备返回值类型与协议点位不一致”；L2 BAD 卡片生成“上次值…当前不可用”及两个不同时间标签。
- [ ] 更新历史模型测试：BIT 的实时/历史 INT 0/1 原样显示，不经过 truthy/boolean 格式化。
- [ ] 运行：

```powershell
cd frontend
node --test src/components/data-trunk/inlinePointProcessingModel.test.mjs src/components/data-trunk/dataTrunkViewModel.test.mjs src/components/rawPointHistoryModel.test.mjs
```

确认现有 identity formula/布尔显示导致 RED。
- [ ] 扩展 API 类型；在 L0 表格同时显示原始值、协议类型 BIT、质量、原因、数据时间和来源。不得新增“规范值”列。
- [ ] 在加工面板加入“0/1 转布尔”和“1 表示 true / 0 表示 true”；检查结果预览显示“设备原值 0 → 原值等于 1 → 实体值 false”。普通错误优先中文，不以英文错误码作为唯一提示。
- [ ] 在实体卡片分开显示 retained value 时间和当前异常时间；质量徽标必须取当前 BAD/STALE，不能取旧值状态。
- [ ] 重跑专项；再运行 `node --test src/**/*.test.mjs e2e/support/*.test.mjs`。PowerShell 若不展开 `**`，先用 `Get-ChildItem -Recurse -Filter *.test.mjs | ForEach-Object FullName` 生成同一完整文件列表传给 `node --test`，不得缩小覆盖范围。
- [ ] 运行 `npm run build`，确认 TypeScript 与 Vite production build 通过。
- [ ] 提交：`git add frontend/src/api/client.ts frontend/src/components/NodeTagPanel.tsx frontend/src/components/data-trunk/inlinePointProcessingModel.ts frontend/src/components/data-trunk/InlinePointProcessingPanel.tsx frontend/src/components/data-trunk/dataTrunkViewModel.ts frontend/src/components/data-trunk/EntityObservationCard.tsx frontend/src/components/data-trunk/inlinePointProcessingModel.test.mjs frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs frontend/src/components/rawPointHistoryModel.test.mjs && git commit -m "feat(ui): expose raw BIT and explicit boolean mapping"`

---

### Task 8: 把主干验收扩展为 BIT 原值与显式加工闭环

**Files:**

- Modify: `frontend/e2e/support/e2eFixture.ts`
- Modify: `frontend/e2e/support/e2eFixture.test.mjs`
- Modify: `frontend/e2e/node-management.spec.ts`
- Modify: `docs/acceptance-checklist.md`

**Interfaces:**

E2E fixture 增加一个协议类型 BIT 的测试点位和纯 MQTT 测试发布能力：

```ts
publishRawPoint(pointKey: string, value: number | string | boolean): Promise<void>
```

测试资源继续放在隔离的 `E2E验证` 节点下，finally 必须通过正式 API 清理配置；禁止控制、自动策略或设备写。

- [ ] 先写 fixture contract test，确保发布 JSON 数字 0/1 时不会被测试工具转成 bool/string，并保持唯一 source timestamp/message id。
- [ ] 在主干 E2E 增加同一条顺序场景：创建节点 → 导入 BIT 点位 → 发布数字 0 → L0 实时显示 `0/BIT/正常` → L0 历史显示 0 → 选择“0/1 转布尔，1 表示 true” → 检查结果 false → 发布实体 → L2 实时 false、历史与来源可见。
- [ ] 继续发布数字 1，等待新 committed frame，断言 L0 为 1、L2 为 true；再发布数字 2，断言 L0 保留 2 且 BAD，L2 显示“上次值 true；当前不可用”，并确认告警配置实体选择器能选择该 L2 但不执行动作。
- [ ] 添加 cleanup 断言：临时节点、点位、当前加工、活动实体、规则、Neuron E2E 节点全部为 0；不清理运行配置以外的现场数据。
- [ ] 运行 `npm run test:e2e:node:list`，确认测试可被 Playwright 收集；在本地隔离环境注入现有 E2E 环境配置后运行 `npm run test:e2e:node`，不得把账号密码写入文档或命令历史。
- [ ] 若本地没有完整 Neuron/MQTT 环境，fixture contract 与 Playwright list 必须先通过；完整写入式 E2E 留到 Task 9 的 1 号机维护窗口后执行，状态明确记为 `INCOMPLETE`，不可跳过并宣称完成。
- [ ] 更新验收清单，把“BIT 原值 0/1、显式 boolean_map、异常 2、L2 last-good/current-bad”列为版本门禁。
- [ ] 提交：`git add frontend/e2e/support/e2eFixture.ts frontend/e2e/support/e2eFixture.test.mjs frontend/e2e/node-management.spec.ts docs/acceptance-checklist.md && git commit -m "test(e2e): cover raw BIT processing trunk"`

---

### Task 9: 升级 v0.6.8、执行完整门禁并固定摘要部署 1 号机

**Files:**

- Modify: `VERSION`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `backend/app/main.py`
- Modify: `scripts/test_build_release_images.py`
- Modify: `CODEX_HANDOFF.md`
- Create: `docs/deploy-1号机-v0.6.8-http.md`

**Interfaces:**

- 版本：`0.6.8`
- Schema：`059`
- 部署：GitHub Actions 构建的 ARM64 immutable digest；1 号机继续 `network_mode: host`、`tmpfs: /dev/mqueue`，不启用 Caddy/TLS。
- 回退：只允许恢复维护窗口前的数据库备份，并启动 v0.6.7 固定摘要；禁止 v0.6.7 连接 Schema 059。

- [ ] 运行 `python scripts/bump_version.py 0.6.8`，检查 VERSION、后端和前端版本同步；把 release image 测试期望 schema 从 058 改为 059。
- [ ] 运行 `git diff --check` 与 `rg -n "0\.6\.7|Schema058|schema-058|migration_058" VERSION frontend/package.json backend/app scripts .github`，逐项判断并更新真正属于当前版本的引用，不改历史部署证据。
- [ ] 执行专项测试：Tasks 1–8 中列出的所有模块，任何失败先回到对应 task 修复，不在 release task 绕过。
- [ ] 执行后端完整门禁：

```powershell
cd backend
python -m unittest discover -s tests -v
cd ..
python -m unittest discover -s scripts -p "test_*.py" -v
```

- [ ] 执行前端完整门禁与构建：

```powershell
cd frontend
$testFiles = Get-ChildItem src,e2e/support -Recurse -Filter *.test.mjs | ForEach-Object FullName
node --test $testFiles
npm run build
npm run test:e2e:node:list
```

- [ ] 运行仓库统一验收 `python scripts/verify_delivery.py`；本地没有现场地址时预期只能是 `INCOMPLETE`，但其中后端、scripts、前端、构建项目必须全 PASS。
- [ ] 请求 `superpowers:requesting-code-review` 做规格符合性和代码质量复审；修复所有 Critical/Important，再重跑受影响测试和完整门禁。
- [ ] 提交版本：`git add VERSION frontend/package.json frontend/package-lock.json backend/app/main.py scripts/test_build_release_images.py && git commit -m "chore(release): prepare v0.6.8"`。确认工作树只剩部署证据尚未生成。
- [ ] 推送 main 和 tag `v0.6.8`；等待 `.github/workflows/release-images.yml` 成功，记录 workflow run、commit、ARM64 manifest digest 和 image ID，禁止使用 floating `latest`。
- [ ] 在 1 号机先只读检查磁盘、当前容器、健康、restart、Schema058、host 网络和 `/dev/mqueue`；确认 NanoMQ/Neuron 连通。若根分区余量不足以同时保留新旧镜像和备份，先清理已确认无用的临时镜像/旧运行数据，不删除配置或未验证备份。
- [ ] 创建 Schema058 完整数据库备份，记录绝对路径、字节数、SHA-256，并用 `pg_restore -l` 验证可读；备份当前 release env/compose。
- [ ] 停止 backend 写者；运行 `--inspect`。若报告有 blocker，部署立即停止并保留 v0.6.7 运行，不执行 schema/apply/clear。
- [ ] blocker 为 0 时记录 digest，应用 migration 059，再执行 `--apply --expected-digest`；核对配置修订只增加预期次数、L2 身份与上层引用不变。
- [ ] 执行 `--clear-runtime --expected-config-revision`，核对返回计数；再次查询确认运行数据为空而节点、点位、加工修订、实体身份和上层配置仍在。
- [ ] 用 ARM64 固定摘要启动 v0.6.8 backend，确认 host 网络、`/dev/mqueue`、healthy、restart 0、Schema059；WARMING 期间不应出现 L2 假 GOOD，收到全新 L0 后进入 READY。
- [ ] 观察至少 60 个连续新节拍：最近帧无 FAILED、无半帧，未完成帧龄不增长，outbox 归零，日志无 ERROR/CRITICAL/Traceback/schema failure；核对 BIT L0 的数据库整数列与 API 都为 0/1。
- [ ] 在 1 号机运行无头 `npm run test:e2e:node`，要求全部通过、0 skip、0 retry；失败则保持版本结论 `FAILED/INCOMPLETE` 并修复，不通过重复等待掩盖问题。
- [ ] 使用 `browser:control-in-app-browser` 在已登录页面沿“节点 → L0 实时/历史 → L1 直接使用/0/1 转布尔 → L2 实时/历史/来源 → 告警”做可见抽查；只读或使用隔离 E2E 节点，不执行规则动作、JDM、控制或设备写。
- [ ] 运行 `python scripts/verify_delivery.py --site-url http://e606.hlszh.com:9000/`；只有自动门禁、现场运行、无头主干和可见 Browser 全部通过才记录 `PASSED`。
- [ ] 在 `docs/deploy-1号机-v0.6.8-http.md` 写入 commit/tag/digest、备份证据、Schema/cutover/clear 计数、测试计数、现场健康、Browser 路径、未执行项和回退步骤；更新 `CODEX_HANDOFF.md`。
- [ ] 提交证据：`git add docs/deploy-1号机-v0.6.8-http.md CODEX_HANDOFF.md && git commit -m "docs(deploy): record v0.6.8 acceptance"`，推送 main。

---

## Final Verification Matrix

| 边界 | 必须证明 | 证据 |
|---|---|---|
| Neuron → L0 | BIT 0/1 是 INT；2/字符串/bool 保留但 BAD | raw adapter + PostgreSQL round-trip |
| L0 实时/历史/恢复 | 实际 typed column 决定类型，reason 不丢 | data frame、stream、restart tests |
| L1 直接使用 | 强类型同单位、零次公式编译 | template/conversion/API tests |
| L1 0/1 转布尔 | 两种 trueWhen，唯一安全 evaluator | template/Postgres/runtime tests |
| L2 latest | retained last-good 与 current BAD 分时展示 | Postgres/API/UI tests |
| 机器消费者 | retained value 不能绕过质量门禁 | alarm/JDM/control tests |
| 硬切 | blocker fail closed、L2 身份稳定、配置保留 | cutover script + real Postgres tests |
| 交付 | Schema059、固定 digest、帧/outbox 健康 | 1 号机运行证据 |
| 用户路径 | 节点→L0→L1→L2→告警实际可用 | Playwright + visible Browser |

## Completion Rule

以下任一项缺失都不得宣布完成：Schema 059 数据契约、确定性配置迁移、运行数据清理、后端完整测试、scripts 完整测试、前端完整测试与生产构建、规格复审、固定摘要部署、1 号机连续节拍证据、无头主干和可见 Browser 抽查。页面显示正确、单个接口 200、单元测试局部通过或容器 healthy 均不能单独代表交付成功。
