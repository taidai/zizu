# Dispatch Strategy JDM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有“规则引擎”硬切为可直接交付 EMS 的“调度策略”，以标准 GoRules JDM 对 committed L2 一致快照求值，只生成有序控制意图，并通过现有统一控制和新 committed L2 回读闭环完成安全执行。

**Architecture:** 在现有单体 FastAPI 进程中建立一个 `StrategyRuntime` 深模块，数据变化和共享分钟节拍只调用它；它在一个事务内保存当前决策、关键事件和控制意图 outbox。提交后的 `ControlIntentDispatcher` 复用 `AutomatedControlCommands` 与 `ControlCommandRuntime`，不直接接触 L0、Neuron、MQTT、HTTP 或设备地址。前端只暴露一个“调度策略”页面和一份标准 JDM，首个易用入口是“2充2放”决策表。

**Tech Stack:** Python 3 / FastAPI / psycopg / PostgreSQL + TimescaleDB / GoRules `zen-engine` / React + TypeScript + Vite / Playwright / Docker Compose

**Spec:** `docs/superpowers/specs/2026-09-04-dispatch-strategy-jdm-design.md`

## Global Constraints

- 唯一主干不变：真实节点树 → L0 原始点位 → L1 点位加工 → L2 全局实体 → 告警 / 调度策略 / 控制 / EMS 工作台。
- 调度策略只读 committed L2；L0 只允许统一控制根据已确认绑定写入，策略代码不得查询或携带 L0、Neuron、MQTT、HTTP 信息。
- 正式执行和试算只有标准 GoRules JDM；删除新运行路径中的简化 `{when, actions}`、AST fallback 和第二套解释器。
- 不引入新依赖、新微服务、Redis、Kafka、Cron 系统、模板 CRUD 或第二份“简单规则”数据。
- 数据变化和固定分钟节拍必须共用同一个 `StrategyRuntime.evaluate()`，固定节拍不补跑停机窗口。
- 发布修订不可变；编辑草稿不能影响当前活动修订；发布不等于启用。
- 一个可控 L2 同时只能有一个活动自动策略主人，数据库约束必须是最终防线。
- 数据库事务提交前零设备动作；重复触发、进程重启和 outbox 重放不得产生重复控制。
- 一个有序动作最多尝试三次；每次重验；第三次失败后锁定，不无限重试、不自动回滚。
- 先用自动化和模拟控制跑通；真实 PCS 只在现场门禁全绿时执行 `156.8 → 156.7 → 156.8 kW`，任何一步失败立即停止。
- 每个任务只提交列明的文件，不把现有 `CODEX_HANDOFF.md`、`.release-artifacts/` 和未跟踪复审文档混入提交。

## File Map

### Database and backend

- Create: `init-db/migration_062_dispatch_strategies.sql`
- Create: `backend/app/services/dispatch_strategies.py`
- Create: `backend/app/services/dispatch_strategy_postgres.py`
- Create: `backend/app/services/dispatch_strategy_workers.py`
- Create: `backend/app/api/dispatch_strategies.py`
- Modify: `backend/app/services/gorules_adapter.py`
- Modify: `backend/app/services/automated_control_commands.py`
- Modify: `backend/app/services/committed_l2_jdm_consumer.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/app/main.py`
- Delete after callers are removed: `backend/app/services/jdm_runtime.py`
- Delete after callers are removed: `backend/app/services/jdm_postgres.py`
- Delete after callers are removed: `backend/app/api/rules.py`
- Delete after router removal: `backend/app/api/rule_templates.py`

### Backend tests

- Create: `backend/tests/test_dispatch_strategy_migration_postgres.py`
- Create: `backend/tests/test_dispatch_strategy_model.py`
- Create: `backend/tests/test_dispatch_strategy_postgres.py`
- Create: `backend/tests/test_dispatch_strategy_runtime.py`
- Create: `backend/tests/test_dispatch_strategy_workers.py`
- Create: `backend/tests/test_dispatch_strategy_public_api.py`
- Modify: `backend/tests/test_committed_l2_jdm_consumer.py`
- Modify: `backend/tests/test_data_trunk_startup_gate.py`
- Modify: `backend/tests/postgres_delivery_app.py`
- Modify: `backend/tests/test_delivery_postgres_public_api.py`
- Modify: `backend/tests/test_control_command_runtime.py`
- Delete after replacement: `backend/tests/test_committed_l2_jdm_postgres.py`
- Delete after replacement: `backend/tests/test_jdm_configuration_revision_postgres.py`
- Delete after replacement: `backend/tests/test_jdm_configuration_revision_public_api.py`

### Frontend and acceptance

- Create: `frontend/src/pages/DispatchStrategyPage.tsx`
- Create: `frontend/src/components/dispatch-strategy/dispatchStrategyModel.mjs`
- Create: `frontend/src/components/dispatch-strategy/dispatchStrategyModel.d.mts`
- Create: `frontend/src/components/dispatch-strategy/dispatchStrategyModel.test.mjs`
- Create: `frontend/e2e/dispatch-strategy.spec.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/pages/NodeTreePage.tsx`
- Modify: `frontend/package.json`
- Delete after replacement: `frontend/src/pages/RuleEnginePage.tsx`
- Delete only if `rg` confirms no caller: `frontend/src/components/rule-engine/jdmExecutionModel.mjs`
- Delete only if `rg` confirms no caller: `frontend/src/components/rule-engine/jdmExecutionModel.d.mts`
- Delete only if `rg` confirms no caller: `frontend/src/components/rule-engine/jdmExecutionModel.test.mjs`

### Release evidence

- Modify: `scripts/test_build_release_images.py`
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `docs/acceptance-checklist.md`
- Create during release: `docs/deploy-1号机-v0.8.5-http.md`
- Modify: `backend/scripts/node_management_e2e_fixture.py`
- Modify: `backend/tests/test_node_management_e2e_fixture.py`
- Modify during release: `VERSION`
- Modify during release: `backend/app/VERSION`
- Modify during release: `frontend/package.json`
- Modify during release: `frontend/package-lock.json`
- Modify during release: `backend/pyproject.toml`
- Update but do not stage unrelated prior changes: `CODEX_HANDOFF.md`

---

## Task 1: Add the dispatch-strategy persistence contract

**Files:**

- Create: `init-db/migration_062_dispatch_strategies.sql`
- Create: `backend/tests/test_dispatch_strategy_migration_postgres.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/tests/test_data_trunk_startup_gate.py`
- Modify: `scripts/test_build_release_images.py`

**Interfaces:**

```python
DISPATCH_STRATEGY_TABLES = (
    "t_dispatch_strategies",
    "t_dispatch_strategy_revisions",
    "t_dispatch_strategy_bindings",
    "t_dispatch_strategy_owners",
    "t_dispatch_control_intents",
    "t_dispatch_strategy_events",
)
```

- [ ] Write the failing PostgreSQL migration test. It must apply migrations through 061, apply 062 twice, and assert:
  - all six tables exist;
  - `t_dispatch_strategy_events` is a Timescale hypertable;
  - only one `DRAFT` exists per strategy;
  - `(strategy_id, revision)` and `(revision_id, evaluation_key, action_id)` are unique;
  - `t_dispatch_strategy_owners.entity_instance_id` is a primary key;
  - control intents accept only `PENDING`, `IN_FLIGHT`, `CONFIRMED`, `CANCELLED`, `FAILED`;
  - `t_control_commands.source_type` accepts `strategy`.

```python
def test_migration_is_idempotent_and_installs_contract(self) -> None:
    self.apply_through_061()
    self.apply_062()
    self.apply_062()
    self.assertEqual(
        self.public_tables(),
        {
            "t_dispatch_strategies",
            "t_dispatch_strategy_revisions",
            "t_dispatch_strategy_bindings",
            "t_dispatch_strategy_owners",
            "t_dispatch_control_intents",
            "t_dispatch_strategy_events",
        },
    )
    self.assertTrue(self.is_hypertable("t_dispatch_strategy_events"))
    self.assertTrue(self.control_source_type_is_allowed("strategy"))
```

- [ ] Run the test and verify it fails because migration 062 does not exist:

```powershell
Set-Location backend
python -m unittest tests.test_dispatch_strategy_migration_postgres -v
```

- [ ] Add migration 062 with these columns and constraints:
  - `t_dispatch_strategies`: stable identity, name, description, `active_revision_id`, `enabled`, `runtime_health`, last evaluation/desired/evidence/failure fields, timestamps;
  - `t_dispatch_strategy_revisions`: strategy, monotonic revision, `DRAFT|PUBLISHED`, `DATA_CHANGE|FIXED_TICK`, IANA timezone, standard JDM JSON, SHA-256 digest, base configuration revision, author/publisher evidence;
  - `t_dispatch_strategy_bindings`: revision, `INPUT|OUTPUT`, stable field/action key, ordered position, L2 entity instance, expected type, unit, freshness seconds;
  - `t_dispatch_strategy_owners`: one active owner row per output L2;
  - `t_dispatch_control_intents`: ordered `SET` payload, evaluation key, attempt count `0..3`, command ID, eligibility time and terminal reason;
  - `t_dispatch_strategy_events`: event time/ID composite primary key, strategy/revision, event type, trigger evidence, snapshot evidence, decision and intent summary;
  - amend the existing control source check to include `strategy` without removing historical source values.

- [ ] Extend the startup schema gate to require migration 062 tables, required columns, constraints and the strategy-events hypertable. Remove `t_jdm_executions` from the active startup contract because the new runtime no longer writes it; do not remove the existing L0/L2/frame/alarm checks.

- [ ] Change the release image schema assertion from 061 to 062 and add an assertion that the 062 migration is copied into the backend image context.

- [ ] Run focused tests and verify green:

```powershell
Set-Location backend
python -m unittest tests.test_dispatch_strategy_migration_postgres tests.test_data_trunk_startup_gate -v
Set-Location ..
python -m unittest scripts.test_build_release_images -v
```

- [ ] Commit only Task 1 files:

```powershell
git add init-db/migration_062_dispatch_strategies.sql backend/tests/test_dispatch_strategy_migration_postgres.py backend/app/services/data_trunk_postgres.py backend/tests/test_data_trunk_startup_gate.py scripts/test_build_release_images.py
git commit -m "feat(strategy): add persistence contract"
```

---

## Task 2: Make standard GoRules JDM the only decision semantics

**Files:**

- Create: `backend/app/services/dispatch_strategies.py`
- Create: `backend/tests/test_dispatch_strategy_model.py`
- Modify: `backend/app/services/gorules_adapter.py`
- Create: `backend/tests/test_gorules_adapter.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class StrategyInput:
    field_key: str
    entity_instance_id: UUID
    value: object
    data_type: str
    unit: str | None
    quality: str
    observed_at: datetime
    frame_sequence: int
    configuration_revision: int

@dataclass(frozen=True)
class ControlIntentDraft:
    action_id: str
    entity_instance_id: UUID
    value: object
    ordinal: int

@dataclass(frozen=True)
class StrategyEvaluation:
    matched_rules: tuple[str, ...]
    decision: Mapping[str, object]
    intents: tuple[ControlIntentDraft, ...]

def compile_standard_jdm(content: Mapping[str, object]) -> str: ...
def evaluate_standard_jdm(
    content: Mapping[str, object],
    inputs: Mapping[str, object],
) -> Mapping[str, object]: ...
def build_two_charge_two_discharge_jdm(
    rows: Sequence[DispatchWindow],
    safe_target: Decimal,
) -> Mapping[str, object]: ...
```

- [ ] First add failing tests for the domain validator and strict adapter:
  - valid standard JDM compiles and evaluates;
  - missing `zen-engine` raises an explicit unavailable error;
  - simplified `{when, actions}` is rejected;
  - AST expressions are rejected instead of locally evaluated;
  - action types other than ordered `SET` are rejected;
  - output target, type and unit must be statically bound;
  - two-charge/two-discharge windows reject overlap and unsplit cross-midnight ranges;
  - an explicit other-time safe target is mandatory;
  - SOC lower bound cannot exceed upper bound;
  - the builder produces a standard JDM decision table, not parallel rule JSON.

```python
def test_legacy_simplified_rule_is_not_executable(self) -> None:
    content = {"when": {"eq": ["soc", 50]}, "actions": []}
    with self.assertRaisesRegex(StandardJdmError, "STANDARD_JDM_REQUIRED"):
        compile_standard_jdm(content)

def test_two_charge_two_discharge_builder_returns_standard_jdm(self) -> None:
    model = build_two_charge_two_discharge_jdm(
        rows=self.valid_rows(),
        safe_target=Decimal("0.0"),
    )
    self.assertEqual(model["contentType"], "application/vnd.gorules.decision")
    self.assertIn("nodes", model)
    self.assertNotIn("when", model)
    self.assertNotIn("actions", model)
```

- [ ] Run and verify the new tests fail on the current fallback behavior:

```powershell
Set-Location backend
python -m unittest tests.test_dispatch_strategy_model tests.test_gorules_adapter -v
```

- [ ] Implement immutable value objects, typed validation errors, the built-in 2-charge/2-discharge builder, strict action extraction and strict `zen-engine` calls. Keep the adapter narrow: compile, evaluate, and extract ordered `SET` results.

- [ ] Remove the AST evaluator and simplified rule evaluation from the strategy-callable path. Historical rows may remain in the database, but no runtime function may execute them.

- [ ] Add a source guard test that scans the new strategy module and adapter for forbidden calls/imports: L0 repositories, Neuron client, MQTT publisher, HTTP client and alarm repository.

- [ ] Run focused tests and verify green:

```powershell
Set-Location backend
python -m unittest tests.test_dispatch_strategy_model tests.test_gorules_adapter -v
```

- [ ] Commit Task 2 files:

```powershell
git add backend/app/services/dispatch_strategies.py backend/app/services/gorules_adapter.py backend/tests/test_dispatch_strategy_model.py backend/tests/test_gorules_adapter.py
git commit -m "refactor(strategy): require standard JDM"
```

---

## Task 3: Implement draft, publish, enable, disable and ownership transactions

**Files:**

- Create: `backend/app/services/dispatch_strategy_postgres.py`
- Create: `backend/tests/test_dispatch_strategy_postgres.py`

**Interfaces:**

```python
class PostgresStrategyRepository:
    def create_strategy(self, draft: StrategyDraft, actor: str) -> StrategyView: ...
    def save_draft(
        self,
        strategy_id: UUID,
        draft: StrategyDraft,
        expected_digest: str,
        actor: str,
    ) -> StrategyView: ...
    def publish(
        self,
        strategy_id: UUID,
        expected_digest: str,
        expected_configuration_revision: int,
        actor: str,
    ) -> StrategyRevision: ...
    def enable(self, strategy_id: UUID, revision_id: UUID, actor: str) -> StrategyView: ...
    def disable(self, strategy_id: UUID, actor: str) -> StrategyView: ...
    def clear_failure(self, strategy_id: UUID, actor: str) -> StrategyView: ...
    def list_strategies(self) -> tuple[StrategyView, ...]: ...
    def get_strategy(self, strategy_id: UUID) -> StrategyView: ...
```

- [ ] Add PostgreSQL tests that prove:
  - create makes one stable strategy plus one draft;
  - saving edits only the draft and uses digest compare-and-swap;
  - publishing freezes a new immutable revision and leaves it disabled;
  - editing an enabled strategy creates/updates a draft while its old active revision stays unchanged;
  - enable acquires all output owners atomically or acquires none;
  - replacing an active revision is rejected while any old intent/control is unfinished;
  - safe replacement atomically swaps owner rows and active pointer;
  - disable stops new evaluation, deletes owner rows and marks only `PENDING` intents `CANCELLED`;
  - `IN_FLIGHT` intents remain for final reconciliation;
  - clearing `FAILED` requires a still-valid active revision and does not fabricate a success event.

```python
def test_second_strategy_cannot_partially_acquire_shared_output(self) -> None:
    first = self.enabled_strategy(outputs=(self.output_a,))
    second = self.published_strategy(outputs=(self.output_a, self.output_b))
    with self.assertRaisesRegex(StrategyOwnershipError, "OUTPUT_ALREADY_OWNED"):
        self.repository.enable(second.id, second.published_revision_id, "engineer")
    self.assertEqual(self.owner_of(self.output_a), first.id)
    self.assertIsNone(self.owner_of(self.output_b))
```

- [ ] Run and verify the repository tests fail:

```powershell
Set-Location backend
python -m unittest tests.test_dispatch_strategy_postgres -v
```

- [ ] Implement every lifecycle method as a single explicit PostgreSQL transaction. Lock the stable strategy row before publish/enable/disable; lock all output entity IDs in sorted order before ownership changes to avoid deadlocks.

- [ ] At publish time call Task 2 validation plus existing entity/control binding validators. Reject non-L2 input, non-controllable output, missing unique confirmed L0 write binding, type/unit mismatch, out-of-limit target, circular input/output ambiguity, stale configuration revision and changed draft digest.

- [ ] Keep `t_rules`, `t_rule_templates` and `t_jdm_executions` outside this runtime. Do not add compatibility reads or dual writes.

- [ ] Run focused repository tests and verify green:

```powershell
Set-Location backend
python -m unittest tests.test_dispatch_strategy_postgres -v
```

- [ ] Commit Task 3 files:

```powershell
git add backend/app/services/dispatch_strategy_postgres.py backend/tests/test_dispatch_strategy_postgres.py
git commit -m "feat(strategy): add revision lifecycle"
```

---

## Task 4: Evaluate coherent committed-L2 snapshots through one runtime

**Files:**

- Modify: `backend/app/services/dispatch_strategies.py`
- Modify: `backend/app/services/dispatch_strategy_postgres.py`
- Create: `backend/tests/test_dispatch_strategy_runtime.py`
- Modify: `backend/app/services/committed_l2_jdm_consumer.py`
- Modify: `backend/tests/test_committed_l2_jdm_consumer.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class StrategyTrigger:
    kind: Literal["DATA_CHANGE", "FIXED_TICK"]
    trigger_key: str
    evaluated_at: datetime
    frame_sequence: int

@dataclass(frozen=True)
class StrategySnapshot:
    frame_sequence: int
    configuration_revision: int
    evaluated_at: datetime
    inputs: tuple[StrategyInput, ...]

class StrategyRuntime:
    def simulate(
        self,
        revision_id: UUID,
        overrides: Mapping[str, object],
        evaluated_at: datetime,
    ) -> StrategyEvaluation: ...

    def evaluate(self, strategy_id: UUID, trigger: StrategyTrigger) -> EvaluationResult: ...

    def evaluate_data_change(self, event: FrameOutboxEvent) -> tuple[EvaluationResult, ...]: ...
```

- [ ] Write failing tests for one public runtime seam:
  - inputs last changed in different frames are loaded at or before one fixed head and form a complete snapshot;
  - the trigger’s changes identify affected strategies but are not treated as the full input set;
  - missing, `BAD`, `UNCERTAIN`, `STALE`, wrong configuration revision, wrong type and wrong unit each return `BLOCKED` and create zero intent;
  - an unchanged block reason produces no second event; a changed reason creates `BLOCK_REASON_CHANGED`; recovery creates one `RECOVERED`;
  - data-change and fixed-tick triggers with the same snapshot produce the same JDM decision;
  - a repeated `trigger_key` returns the persisted result and creates no duplicate intent;
  - simulation uses the same snapshot/evaluator, applies explicit typed overrides, and performs no writes;
  - unchanged desired value plus actual at target makes no intent;
  - unchanged desired value plus drift creates one reconcile intent only when there is no in-flight intent or failure latch;
  - transaction failure persists neither state/event nor intent.

```python
def test_trigger_changes_are_locator_not_the_whole_snapshot(self) -> None:
    repository = FakeStrategyRepository(
        inputs={"soc": self.sample(49, frame=40), "limit": self.sample(156.8, frame=42)}
    )
    runtime = StrategyRuntime(repository, self.engine)
    result = runtime.evaluate(self.strategy_id, self.data_change_trigger(frame=42))
    self.assertEqual(result.snapshot.frame_sequence, 42)
    self.assertEqual(result.engine_inputs, {"soc": 49, "limit": 156.8})
```

- [ ] Run and verify the tests fail:

```powershell
Set-Location backend
python -m unittest tests.test_dispatch_strategy_runtime tests.test_committed_l2_jdm_consumer -v
```

- [ ] Implement a bound-entity snapshot query in `PostgresStrategyRepository`. It must choose a committed terminal frame head first, then read each bound `t_l2_latest` row only when `frame_sequence <= head`; calculate effective quality with the same freshness rules as the committed-frame stream.

- [ ] Implement `StrategyRuntime` so `evaluate()` performs exactly: load active immutable revision → freeze head/configuration/time → load and validate complete bound snapshot → evaluate standard JDM → compare desired/actual → transactionally update current state, append only meaningful events, and insert idempotent intent rows.

- [ ] Refocus `CommittedL2JdmConsumer` into a thin data-change adapter that calls `StrategyRuntime.evaluate_data_change(event)`. It must not evaluate JDM or save legacy executions itself.

- [ ] Run focused tests and verify green:

```powershell
Set-Location backend
python -m unittest tests.test_dispatch_strategy_runtime tests.test_committed_l2_jdm_consumer -v
```

- [ ] Commit Task 4 files:

```powershell
git add backend/app/services/dispatch_strategies.py backend/app/services/dispatch_strategy_postgres.py backend/app/services/committed_l2_jdm_consumer.py backend/tests/test_dispatch_strategy_runtime.py backend/tests/test_committed_l2_jdm_consumer.py
git commit -m "feat(strategy): evaluate committed L2 snapshots"
```

---

## Task 5: Dispatch committed intents through unified control and recover after restart

**Files:**

- Create: `backend/app/services/dispatch_strategy_workers.py`
- Create: `backend/tests/test_dispatch_strategy_workers.py`
- Modify: `backend/app/services/automated_control_commands.py`
- Modify: `backend/tests/test_control_command_runtime.py`
- Modify: `backend/app/main.py`

**Interfaces:**

```python
class FixedMinuteTickWorker:
    async def run(self, stop: asyncio.Event) -> None: ...
    async def run_once(self, tick_at: datetime) -> int: ...

class ControlIntentDispatcher:
    async def run(self, stop: asyncio.Event) -> None: ...
    def run_once(self, now: datetime) -> DispatchResult | None: ...
    def recover(self, now: datetime) -> int: ...
```

- [ ] Add failing worker/control tests for:
  - one shared minute boundary calls the same `StrategyRuntime.evaluate()` used by data changes;
  - restart starts at the next minute boundary and never emits missed historical ticks;
  - dispatcher only claims committed `PENDING` rows with `FOR UPDATE SKIP LOCKED` semantics;
  - a later ordinal remains pending until the previous ordinal has `CONFIRMED` L2 readback;
  - dispatch rechecks enabled revision, owner, configuration revision and intent eligibility before calling unified control;
  - automated control accepts canonical `source_type="strategy"` and derives idempotency from intent ID plus attempt number;
  - an existing in-flight attempt is reconciled, not re-submitted;
  - command readback confirmation marks the intent confirmed and releases the next ordinal;
  - a terminal command failure schedules at most two more attempts using persisted command timeout/cooldown evidence;
  - the third terminal failure marks intent and strategy `FAILED`, cancels remaining sequence and emits one failure event/alarm;
  - a failure-latched strategy does not retry on later minute ticks;
  - disable cancels pending rows while already-dispatched rows finish reconciliation;
  - restart recovers committed pending/in-flight work without duplicate commands.

```python
def test_third_failed_attempt_latches_strategy(self) -> None:
    dispatcher = self.dispatcher(failing_control=True)
    dispatcher.run_until_idle(self.intent_id)
    intent = self.repository.get_intent(self.intent_id)
    strategy = self.repository.get_strategy(self.strategy_id)
    self.assertEqual(intent.attempt_count, 3)
    self.assertEqual(intent.status, "FAILED")
    self.assertEqual(strategy.runtime_health, "FAILED")
    self.assertEqual(self.control.submit_count, 3)
```

- [ ] Run and verify failures:

```powershell
Set-Location backend
python -m unittest tests.test_dispatch_strategy_workers tests.test_control_command_runtime -v
```

- [ ] Add `strategy` to the automated-control request type and evidence contract. Keep all existing permission, range, interlock, cooldown, unique L0 write-point and readback checks in `ControlCommandRuntime`; do not duplicate them in the strategy worker.

- [ ] Implement one minute worker and one intent-dispatch/reconcile worker in the existing FastAPI process. Register both in the current startup/shutdown task group, share the same repository/runtime instance as the committed-L2 consumer, and call `recover()` before the loops start.

- [ ] Derive attempt idempotency exactly as `sha256(f"{intent_id}:{attempt_number}")`. Persist the control command ID before advancing; use new committed L2 readback from the existing control runtime as the only success signal.

- [ ] Use the existing alarm runtime seam for the single final-failure alarm; no JDM action may write alarms directly.

- [ ] Run focused tests and verify green:

```powershell
Set-Location backend
python -m unittest tests.test_dispatch_strategy_workers tests.test_control_command_runtime -v
```

- [ ] Commit Task 5 files:

```powershell
git add backend/app/services/dispatch_strategy_workers.py backend/app/services/automated_control_commands.py backend/app/main.py backend/tests/test_dispatch_strategy_workers.py backend/tests/test_control_command_runtime.py
git commit -m "feat(strategy): close control intent loop"
```

---

## Task 6: Replace legacy rule APIs with one dispatch-strategy API

**Files:**

- Create: `backend/app/api/dispatch_strategies.py`
- Create: `backend/tests/test_dispatch_strategy_public_api.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/postgres_delivery_app.py`
- Modify: `backend/tests/test_delivery_postgres_public_api.py`
- Delete: `backend/app/api/rules.py`
- Delete: `backend/app/api/rule_templates.py`
- Delete: `backend/app/services/jdm_runtime.py`
- Delete: `backend/app/services/jdm_postgres.py`
- Delete: `backend/tests/test_committed_l2_jdm_postgres.py`
- Delete: `backend/tests/test_jdm_configuration_revision_postgres.py`
- Delete: `backend/tests/test_jdm_configuration_revision_public_api.py`

**Public API:**

```text
GET    /api/v1/dispatch-strategies
POST   /api/v1/dispatch-strategies
GET    /api/v1/dispatch-strategies/{strategy_id}
PUT    /api/v1/dispatch-strategies/{strategy_id}/draft
POST   /api/v1/dispatch-strategies/{strategy_id}/simulate
POST   /api/v1/dispatch-strategies/{strategy_id}/publish
POST   /api/v1/dispatch-strategies/{strategy_id}/enable
POST   /api/v1/dispatch-strategies/{strategy_id}/disable
POST   /api/v1/dispatch-strategies/{strategy_id}/failure-latch/clear
GET    /api/v1/dispatch-strategies/{strategy_id}/events
```

- [ ] Add failing API tests for role and lifecycle behavior:
  - operator can list/detail/simulate/read events but cannot create/edit/publish/enable/disable/clear;
  - engineer can use the whole lifecycle;
  - create selects the single server-owned 2-charge/2-discharge starter and returns a draft;
  - draft update requires `expected_digest`;
  - simulate returns input values, quality, timestamps, matched row, decision and proposed intents, but intent count in DB remains unchanged;
  - publish requires expected digest and configuration revision and returns a new immutable revision;
  - enable requires an explicit published revision ID;
  - events use bounded cursor pagination and never return per-tick no-op entries;
  - `/api/v1/rules` and `/api/v1/rule-templates` are absent after the hard cut.

```python
async def test_simulation_has_no_side_effect(self) -> None:
    before = self.repository.intent_count()
    response = await self.client.post(
        f"/api/v1/dispatch-strategies/{self.strategy_id}/simulate",
        json={"overrides": {"soc": 51.0}},
        headers=self.engineer_headers,
    )
    self.assertEqual(response.status_code, 200)
    self.assertEqual(self.repository.intent_count(), before)
    self.assertEqual(response.json()["snapshot"]["soc"]["value"], 51.0)
```

- [ ] Run and verify failures:

```powershell
Set-Location backend
python -m unittest tests.test_dispatch_strategy_public_api -v
```

- [ ] Implement Pydantic request/response models and map typed domain errors to stable HTTP problem codes. The API must not expose JDM side-effect actions, device addresses, raw L0 or HTTP targets.

- [ ] Switch `main.py`, the PostgreSQL delivery test app and delivery API tests to the new router. Remove old rules/template routers and delete their API/repository/runtime implementations and superseded tests because retaining executable legacy services would preserve the forbidden second semantics.

- [ ] Confirm no production caller remains before deletion:

```powershell
rg -n "api\.rules|rules_router|rule_templates_router|jdm_runtime|jdm_postgres|/rules|/rule-templates" backend frontend/src
```

- [ ] Run focused and router smoke tests:

```powershell
Set-Location backend
python -m unittest tests.test_dispatch_strategy_public_api tests.test_release_lock_health -v
```

- [ ] Commit Task 6 files:

```powershell
git add backend/app/api/dispatch_strategies.py backend/app/main.py backend/tests/postgres_delivery_app.py backend/tests/test_delivery_postgres_public_api.py backend/tests/test_dispatch_strategy_public_api.py
git add -u backend/app/api/rules.py backend/app/api/rule_templates.py backend/app/services/jdm_runtime.py backend/app/services/jdm_postgres.py backend/tests/test_committed_l2_jdm_postgres.py backend/tests/test_jdm_configuration_revision_postgres.py backend/tests/test_jdm_configuration_revision_public_api.py
git commit -m "feat(strategy): expose lifecycle API"
```

---

## Task 7: Build the single-page 2-charge/2-discharge workflow

**Files:**

- Create: `frontend/src/pages/DispatchStrategyPage.tsx`
- Create: `frontend/src/components/dispatch-strategy/dispatchStrategyModel.mjs`
- Create: `frontend/src/components/dispatch-strategy/dispatchStrategyModel.d.mts`
- Create: `frontend/src/components/dispatch-strategy/dispatchStrategyModel.test.mjs`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/pages/NodeTreePage.tsx`
- Delete: `frontend/src/pages/RuleEnginePage.tsx`
- Delete only after zero references: `frontend/src/components/rule-engine/jdmExecutionModel.mjs`
- Delete only after zero references: `frontend/src/components/rule-engine/jdmExecutionModel.d.mts`
- Delete only after zero references: `frontend/src/components/rule-engine/jdmExecutionModel.test.mjs`

**User flow:**

```text
策略列表 → 新建“2充2放” → 绑定 L2 → 填决策表 → 试算
        → 发布 → 启用 → 看关键事件/控制回读 → 停用
```

- [ ] Write failing pure-model tests for:
  - local-time windows sort deterministically;
  - overlaps are highlighted before submit;
  - cross-midnight rows are split into two standard rows;
  - other-time safe target is required;
  - typed L2 bindings preserve entity instance ID, type, unit and freshness;
  - API validation errors map to one plain-Chinese field message;
  - the strategy status view keeps revision lifecycle, enable state and runtime health separate.

```javascript
test('跨午夜时间段拆成当天尾段和次日首段', () => {
  assert.deepEqual(splitCrossMidnight({ start: '22:00', end: '02:00' }), [
    { start: '22:00', end: '24:00' },
    { start: '00:00', end: '02:00' },
  ])
})
```

- [ ] Run and verify failure:

```powershell
Set-Location frontend
node src/components/dispatch-strategy/dispatchStrategyModel.test.mjs
```

- [ ] Add typed client methods for every Task 6 endpoint. Do not retain active UI calls to `getRules`, rule-template CRUD or direct `submitControlCommand` from the strategy editor.

- [ ] Build one page with four visible zones:
  - compact strategy list with name, trigger, outputs, published revision, enable state, health, last evaluation, desired/actual and last result;
  - L2 binding panel restricted to valid global entities, showing quality/unit/freshness and controllability;
  - 2-charge/2-discharge decision table with start/end, charge/discharge/hold, target, SOC lower/upper and mandatory other-time safe target;
  - simulation and event/control result panel showing input evidence, hit row, decision, proposed intent, command state and L2 readback.

- [ ] Keep one optional “打开完整规则图” action that edits the same standard JDM document. Do not create a second mode, model conversion, generic action editor or template-management page.

- [ ] Change the navigation label from “规则引擎” to “调度策略”; update the node page hint to point to “调度策略”. Lazy-load the new page.

- [ ] Delete the legacy page because it exposes forbidden simplified actions, direct test-write controls and two editing modes. Delete old helper files only after this command reports zero imports:

```powershell
rg -n "RuleEnginePage|jdmExecutionModel|getRules|getRuleTemplates|submitControlCommand" frontend/src
```

- [ ] Run all frontend contract tests and build:

```powershell
Set-Location frontend
Get-ChildItem src -Recurse -Filter '*.test.mjs' | ForEach-Object { node $_.FullName }
npm run build
```

- [ ] Commit Task 7 files without staging unrelated files:

```powershell
git add frontend/src/pages/DispatchStrategyPage.tsx frontend/src/components/dispatch-strategy/dispatchStrategyModel.mjs frontend/src/components/dispatch-strategy/dispatchStrategyModel.d.mts frontend/src/components/dispatch-strategy/dispatchStrategyModel.test.mjs frontend/src/api/client.ts frontend/src/App.tsx frontend/src/pages/NodeTreePage.tsx
git add -u frontend/src/pages/RuleEnginePage.tsx frontend/src/components/rule-engine/jdmExecutionModel.mjs frontend/src/components/rule-engine/jdmExecutionModel.d.mts frontend/src/components/rule-engine/jdmExecutionModel.test.mjs
git commit -m "feat(strategy): add 2-charge-2-discharge UI"
```

---

## Task 8: Add headless vertical-slice acceptance and architecture guards

**Files:**

- Create: `frontend/e2e/dispatch-strategy.spec.ts`
- Modify: `frontend/package.json`
- Modify: `backend/scripts/node_management_e2e_fixture.py`
- Modify: `backend/tests/test_node_management_e2e_fixture.py`
- Modify: `docs/acceptance-checklist.md`
- Modify: `README.md`
- Modify: `README_EN.md`

- [ ] First add a failing Playwright specification that logs in and proves the UI sequence:
  - open “调度策略”;
  - create the built-in 2-charge/2-discharge strategy;
  - bind test L2 inputs and one controllable L2 output;
  - fill non-overlapping rows and safe other-time target;
  - simulate and assert snapshot evidence/hit row/proposed intent;
  - publish and assert immutable revision;
  - enable and assert `ENABLED / READY`;
  - inject a deterministic committed-L2 test event through the existing E2E fixture seam;
  - assert exactly one strategy event and one control command/readback result;
  - disable and assert no later trigger creates an intent.

- [ ] Add a second headless scenario that walks the complete product main trunk:

```text
节点树 → 原始数据 L0 → 点位加工 L1 → 标准实体 L2 → 告警 → 调度策略
```

It must assert no browser console error and must inspect the L2 identity used by alarm and strategy rather than relying on page visibility alone.

- [ ] Replace the old `/rules` setup/cleanup in `node_management_e2e_fixture.py` with the new draft → simulate → publish → enable → disable strategy lifecycle. Update its unit test to assert the exact new routes and standard JDM payload; do not retain a hidden legacy rule fixture.

- [ ] Add a package script:

```json
{
  "test:e2e:dispatch-strategy": "playwright test e2e/dispatch-strategy.spec.ts"
}
```

- [ ] Run against the local stack and verify the first run fails at the earliest incomplete seam:

```powershell
Set-Location frontend
npm run test:e2e:dispatch-strategy
```

- [ ] Make only fixture/selector/accessibility corrections required by the acceptance test. Do not weaken assertions or add product behavior not present in the accepted spec.

- [ ] Add source-level architecture assertions to an existing/new backend test:

```python
FORBIDDEN_STRATEGY_TOKENS = (
    "ast.parse",
    'content.get("when")',
    "NeuronClient",
    "mqtt.publish",
    "requests.post",
    "httpx.post",
    "t_l0_latest",
)
```

The scan is limited to strategy runtime, API and workers; unified control remains allowed to resolve the confirmed L0 write binding.

- [ ] Update the acceptance checklist and both READMEs so the user-facing module is “调度策略”; document the exact configuration flow and state that JDM is internal sole semantics.

- [ ] Run headless acceptance, frontend contracts and build:

```powershell
Set-Location frontend
npm run test:e2e:dispatch-strategy
Get-ChildItem src -Recurse -Filter '*.test.mjs' | ForEach-Object { node $_.FullName }
npm run build
```

- [ ] Commit Task 8 files:

```powershell
git add frontend/e2e/dispatch-strategy.spec.ts frontend/package.json backend/scripts/node_management_e2e_fixture.py backend/tests/test_node_management_e2e_fixture.py docs/acceptance-checklist.md README.md README_EN.md
git commit -m "test(strategy): cover delivery workflow"
```

---

## Task 9: Verify, release v0.8.5, deploy to host 1 and perform guarded PCS closure

**Files:**

- Modify: `VERSION`
- Modify: `backend/app/VERSION`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `backend/pyproject.toml`
- Create: `docs/deploy-1号机-v0.8.5-http.md`
- Update without staging unrelated prior content: `CODEX_HANDOFF.md`

- [ ] Run placeholder and legacy-path scans before claiming completion:

```powershell
rg -n "TODO|TBD|placeholder|NotImplementedError" backend/app/services/dispatch_strategies.py backend/app/services/dispatch_strategy_postgres.py backend/app/services/dispatch_strategy_workers.py backend/app/api/dispatch_strategies.py frontend/src/pages/DispatchStrategyPage.tsx
rg -n "api\.rules|rule_templates_router|RuleEnginePage|content\.get\(\"when\"\)|ast\.parse" backend/app backend/tests frontend/src
```

Expected: no implementation placeholders; no production legacy rule router/page/fallback references. Historical migration names and explanatory tests may remain.

- [ ] Run the complete local verification suite:

```powershell
Set-Location backend
python -m unittest discover -s tests -p 'test_*.py'
Set-Location ..
python -m unittest discover -s scripts -p 'test_*.py'
Set-Location frontend
Get-ChildItem src -Recurse -Filter '*.test.mjs' | ForEach-Object { node $_.FullName }
npm run build
npm run test:e2e:dispatch-strategy
```

- [ ] Bump all three version sources to `0.8.5` with the repository script, run the version tests, and commit:

```powershell
Set-Location ..
python scripts/bump_version.py patch
python -m unittest scripts.test_bump_version -v
git add VERSION backend/app/VERSION backend/pyproject.toml frontend/package.json frontend/package-lock.json
git commit -m "chore(release): prepare v0.8.5"
```

- [ ] Perform an independent review of the accepted spec versus the branch diff. Fix every Critical/Important finding, rerun affected tests, then rerun the full suite.

- [ ] Create and push annotated tag `v0.8.5`; wait for the GitHub Actions ARM64 image build to finish and record the immutable digest. Do not deploy a moving tag.

- [ ] On host 1, before mutation, record disk space, current container/image digest, backend health, restart count, schema version, NanoMQ/Neuron status, host-network mode and `/dev/mqueue` tmpfs. Back up configuration plus strategy/control data and verify the backup can be listed/restored in isolation.

- [ ] Pull and deploy the exact v0.8.5 digest using the existing host-1 compose pattern. Keep `network_mode: host`, `/dev/mqueue` tmpfs and the old container’s required configuration. Do not start Caddy or request TLS.

- [ ] Verify after deployment:
  - backend healthy and restart count 0;
  - schema 062 installed;
  - NanoMQ and Neuron remain available;
  - pending strategy intent backlog drains normally;
  - recent logs contain no migration, JDM, dispatcher or control errors;
  - headless main-trunk and dispatch-strategy tests pass against `http://e606.hlszh.com:9000/`.

- [ ] Gate the real PCS check with a fresh read of `en9_pcs/cmd` → “最大放电功率限值” → `1!420601`. Continue only if it is still `156.8 kW`, `GOOD`, uniquely bound, RW `INT16` with decimal `0.1`, no active conflicting owner/in-flight command, interlocks permit the test, and the site operator still permits the small setpoint change.

- [ ] Execute the real closure through the strategy API/UI only:

```text
156.8 kW → SET L2 to 156.7 kW → wait for a new committed L2 readback at 156.7
156.7 kW → SET L2 to 156.8 kW → wait for a new committed L2 readback at 156.8
```

Stop immediately if any gate, write, timeout or readback fails. Do not widen the delta and do not claim full 2-charge/2-discharge energy scheduling from this check.

- [ ] Record exact automated counts, browser evidence, database event/intent IDs, control command IDs, both L2 readbacks, image digest, backup path and rollback command in `docs/deploy-1号机-v0.8.5-http.md`.

- [ ] Update `CODEX_HANDOFF.md` with the real final state, but stage only the lines written for this release if the file still contains unrelated user changes.

- [ ] Final completion gate: do not say “完成” unless all of these are evidenced together: standard JDM only, coherent L2 snapshot, transactionally committed intent, unified-control dispatch, committed-L2 readback, lifecycle/ownership rules, local full suite, deployed digest, headless main-trunk pass and guarded PCS restore to 156.8 kW.

---

## Final Self-Review Checklist

- [ ] Every requirement in spec sections 3–14 maps to at least one task and one verification step.
- [ ] No new dependency, service, scheduler engine, template CRUD, simplified rule model or direct device side effect was introduced.
- [ ] `StrategyRuntime` has one evaluation seam for both triggers and one simulation seam with no side effects.
- [ ] Strategy state is not a second realtime truth; current actual values come from committed L2.
- [ ] Events are change-only; no per-minute no-op writes and no duplicated control audit.
- [ ] All destructive file removals are limited to the confirmed hard-cut legacy rule UI/API after zero-caller checks.
- [ ] Existing unrelated working-tree content is still untouched and uncommitted.
