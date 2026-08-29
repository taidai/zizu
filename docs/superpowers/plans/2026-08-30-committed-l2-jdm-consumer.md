# Committed L2 JDM Consumer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 JDM 只按顺序消费已提交 L2 数据帧，原子记录执行或拒绝事实及控制意图，并删除 L0/latest 定时扫描旁路。

**Architecture:** 在现有 `CommittedFrameFanout` 中按“告警 → JDM → 实时流”串行加入一个薄消费者。JDM 运行时只从 `FrameOutboxEvent.l2_changes` 组装已发布模型声明的实体输入；同一事务写 `jdm` 消费收据与全部模型执行事实，重放幂等。JDM 配置写入先通过既有 `ConfigurationRuntimeGate` 排空旧帧，再推进全局配置修订并重建 WARMING 运行态。

**Tech Stack:** Python 3.12、FastAPI、PostgreSQL/TimescaleDB、psycopg2、GoRules `zen-engine`、React/TypeScript、unittest/pytest。

**Spec:** `docs/superpowers/specs/2026-08-28-upper-layer-committed-l2-convergence-design.md`

## Global Constraints

- 只实现规格第 6 节“JDM”切片；不实现自动控制消费者或 EMS 工作台改造。
- 不增加依赖、微服务、中间件、第二套规则引擎、脚本执行器或模块私有帧队列。
- 正式运行输入只能来自 `FrameOutboxEvent.l2_changes`；不得查询 L0、`t_telemetry_latest`、原始 MQTT 或旧全局实体绑定。
- 正式执行只记录判断和控制意图，不写告警表、控制命令表或设备。
- 非 GOOD、缺失输入、旧配置修订和模型错误都持久化稳定拒绝码；它们是成功的 fail-closed 消费，不阻塞后续帧。
- 数据库错误或半笔写入必须整笔回滚并让 outbox 队头重试。
- 1 号机保留 `network_mode: host`、`/dev/mqueue`、HTTP 模式；不执行 JDM 自动策略或真实设备写入。

## File Structure

- `init-db/migration_052_committed_l2_jdm.sql`：为当前 JDM 模型绑定配置修订，并保存帧级执行事实。
- `backend/app/services/jdm_runtime.py`：纯领域模型、输入校验、同一 GoRules adapter 求值、帧事务编排。
- `backend/app/services/jdm_postgres.py`：活动模型读取、`jdm` 收据和执行事实的 PostgreSQL 事务适配器。
- `backend/app/services/committed_l2_jdm_consumer.py`：把 committed frame 异步交给 JDM 运行时。
- `backend/app/api/rules.py`：JDM 配置发布栅栏、模型配置修订、零副作用试运行和执行事实只读接口。
- `backend/app/main.py`：按告警、JDM、实时流顺序注册 fanout。
- `backend/app/services/rule_engine.py`：删除旧 latest/L0 扫描与直接告警/控制执行路径；正式逻辑由 `jdm_runtime.py` 取代。
- `backend/app/services/rule_alarm_adapter.py`：删除 JDM 直写告警适配器；告警继续只由 committed L2 告警消费者驱动。
- `backend/tests/test_committed_l2_jdm_consumer.py`：纯领域 RED/GREEN 行为测试。
- `backend/tests/test_committed_l2_jdm_postgres.py`：真实 PostgreSQL 原子性、重放和恢复测试。
- `backend/tests/test_jdm_configuration_revision_public_api.py`：配置栅栏、修订和只读执行记录 API 测试。
- `backend/tests/test_data_frame_outbox.py`、`backend/tests/test_data_trunk_startup_gate.py`：fanout 顺序和旁路硬切契约。
- `frontend/src/api/client.ts`：JDM 类型只保留 control/linkage，并增加执行事实读取类型。
- `frontend/src/pages/RuleEnginePage.tsx`：规则详情只读显示最近执行/拒绝原因，不自动运行策略。

---

### Task 1: Schema 052 固化模型修订与执行事实

**Files:**
- Create: `init-db/migration_052_committed_l2_jdm.sql`
- Modify: `scripts/test_build_release_images.py`
- Test: `backend/tests/test_committed_l2_jdm_postgres.py`

**Interfaces:**
- Produces: `t_rules.configuration_revision BIGINT NOT NULL`
- Produces: `t_jdm_executions(id, rule_id, rule_version, frame_id, frame_sequence, configuration_revision, model_digest, status, reason_code, inputs, outputs, actions, executed_at)`
- Invariant: `UNIQUE(rule_id, rule_version, frame_id)` and `frame_id → t_data_frames ON DELETE CASCADE`

- [ ] **Step 1: Write the failing replay-safe migration test**

```python
def test_schema_052_is_replayable_and_rejects_partial_jdm_execution(self):
    self.execute_migrations_through_051()
    self.execute_sql(MIGRATION_052)
    self.execute_sql(MIGRATION_052)
    self.assert_column("t_rules", "configuration_revision", nullable=False)
    self.assert_constraint("uq_jdm_execution_rule_frame")
    self.assert_check("chk_jdm_execution_status")
```

- [ ] **Step 2: Run the migration test and verify RED**

Run: `python -m unittest backend.tests.test_committed_l2_jdm_postgres.JdmSchemaPostgresTest -v`

Expected: FAIL because migration 052 and `t_jdm_executions` do not exist.

- [ ] **Step 3: Add the replay-safe migration**

```sql
ALTER TABLE public.t_rules
  ADD COLUMN IF NOT EXISTS configuration_revision BIGINT;
UPDATE public.t_rules
SET configuration_revision=(SELECT current_revision FROM public.t_configuration_state WHERE singleton)
WHERE configuration_revision IS NULL;
ALTER TABLE public.t_rules ALTER COLUMN configuration_revision SET NOT NULL;

CREATE TABLE public.t_jdm_executions (
  id UUID PRIMARY KEY,
  rule_id UUID NOT NULL,
  rule_version INTEGER NOT NULL CHECK (rule_version > 0),
  frame_id UUID NOT NULL REFERENCES public.t_data_frames(frame_id) ON DELETE CASCADE,
  frame_sequence BIGINT NOT NULL CHECK (frame_sequence > 0),
  configuration_revision BIGINT NOT NULL REFERENCES public.t_configuration_revisions(revision),
  model_digest TEXT NOT NULL CHECK (model_digest ~ '^[0-9a-f]{64}$'),
  status TEXT NOT NULL CONSTRAINT chk_jdm_execution_status CHECK (status IN ('executed','rejected')),
  reason_code TEXT,
  inputs JSONB NOT NULL,
  outputs JSONB NOT NULL,
  actions JSONB NOT NULL,
  executed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT uq_jdm_execution_rule_frame UNIQUE(rule_id, rule_version, frame_id),
  CONSTRAINT chk_jdm_execution_reason CHECK (
    (status='executed' AND reason_code IS NULL) OR
    (status='rejected' AND reason_code IS NOT NULL)
  )
);
```

Migration replay must validate the complete footprint and raise `SCHEMA_052_PARTIAL_STRUCTURE` instead of repairing an unknown partial table.

- [ ] **Step 4: Update release Schema assertion and run GREEN**

Run: `python -m unittest backend.tests.test_committed_l2_jdm_postgres.JdmSchemaPostgresTest scripts.test_build_release_images -v`

Expected: PASS and release schema assertion equals `052`.

- [ ] **Step 5: Commit**

```bash
git add init-db/migration_052_committed_l2_jdm.sql backend/tests/test_committed_l2_jdm_postgres.py scripts/test_build_release_images.py
git commit -m "feat(jdm): add committed execution schema"
```

### Task 2: 纯 JDM 帧运行时

**Files:**
- Create: `backend/app/services/jdm_runtime.py`
- Create: `backend/app/services/committed_l2_jdm_consumer.py`
- Test: `backend/tests/test_committed_l2_jdm_consumer.py`

**Interfaces:**
- Produces: `JdmModel(id: UUID, version: int, configuration_revision: int, content: dict[str, Any])`
- Produces: `JdmExecution(id, rule_id, rule_version, frame_id, frame_sequence, configuration_revision, model_digest, status, reason_code, inputs, outputs, actions)`
- Produces: `JdmRuntime.submit_frame(event: FrameOutboxEvent) -> tuple[JdmExecution, ...]`
- Produces: `CommittedL2JdmConsumer.publish(event: FrameOutboxEvent) -> None`

- [ ] **Step 1: Write fail-closed and idempotency tests**

```python
async def test_good_committed_l2_executes_model_and_records_control_intent(self):
    event = frame(change(ENTITY_ID, value=12, quality=TrunkQuality.GOOD))
    model = jdm_model(input_mappings={"power": str(ENTITY_ID)}, when="power > 10")
    await consumer(model).publish(event)
    execution = repository.executions[0]
    self.assertEqual("executed", execution.status)
    self.assertEqual([{"type": "control", "entity_instance_id": str(TARGET_ID), "value": 5}], execution.actions)

async def test_missing_or_stale_required_input_records_rejection_without_evaluation(self):
    await consumer(model).publish(frame(change(ENTITY_ID, quality=TrunkQuality.STALE)))
    self.assertEqual("JDM_INPUT_QUALITY_NOT_GOOD", repository.executions[0].reason_code)

async def test_same_frame_replay_is_noop_after_atomic_commit(self):
    await consumer(model).publish(event)
    await consumer(model).publish(event)
    self.assertEqual(1, len(repository.executions))
```

Also cover `JDM_INPUT_MISSING`, `JDM_INPUT_TIMESTAMP_MISSING`, `JDM_MODEL_CONFIGURATION_MISMATCH`, `JDM_EVALUATION_FAILED`, multiple rules in one frame, and rollback after the second save raises.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest backend.tests.test_committed_l2_jdm_consumer -v`

Expected: import failure for `app.services.jdm_runtime`.

- [ ] **Step 3: Implement typed input and evaluation helpers**

```python
GOOD_QUALITY = int(TrunkQuality.GOOD)

def required_inputs(content: Mapping[str, Any]) -> dict[str, UUID]:
    config = content.get("_config")
    if not isinstance(config, Mapping):
        raise JdmRuntimeError("JDM_MODEL_INVALID")
    mappings = {str(field): UUID(str(value)) for field, value in config.get("inputMappings", {}).items()}
    for value in config.get("sourceEntityInstanceIds", ()):
        entity_id = UUID(str(value))
        mappings.setdefault(str(entity_id), entity_id)
    return mappings

def evaluate_model(model: JdmModel, event: FrameOutboxEvent) -> JdmExecution:
    # Only event.l2_changes may populate context.
    # Rejections return a JdmExecution; persistence errors raise and roll back.
```

Use canonical SHA-256 over model content, UUID5 over `rule/version/frame`, `Decimal` JSON normalization, and `app.services.gorules_adapter.evaluate_rule` as the only model adapter. Do not invoke actions.

- [ ] **Step 4: Implement transaction orchestration and thin async consumer**

```python
class JdmRuntime:
    def submit_frame(self, event: FrameOutboxEvent) -> tuple[JdmExecution, ...]:
        with self._repository.transaction() as transaction:
            if not transaction.begin_committed_frame("jdm", event.frame_id, event.frame_sequence, event.configuration_revision):
                return ()
            executions = tuple(evaluate_model(model, event) for model in transaction.active_models(event.configuration_revision))
            for execution in executions:
                transaction.save_execution(execution)
            return executions

class CommittedL2JdmConsumer:
    async def publish(self, event: FrameOutboxEvent) -> None:
        await asyncio.to_thread(self._runtime.submit_frame, event)
```

- [ ] **Step 5: Run GREEN and commit**

Run: `python -m unittest backend.tests.test_committed_l2_jdm_consumer -v`

```bash
git add backend/app/services/jdm_runtime.py backend/app/services/committed_l2_jdm_consumer.py backend/tests/test_committed_l2_jdm_consumer.py
git commit -m "feat(jdm): consume committed l2 frames"
```

### Task 3: PostgreSQL 原子收据和执行存储

**Files:**
- Create: `backend/app/services/jdm_postgres.py`
- Modify: `backend/app/services/committed_l2_jdm_consumer.py`
- Test: `backend/tests/test_committed_l2_jdm_postgres.py`

**Interfaces:**
- Produces: `PostgresJdmRepository.transaction()` context manager
- Transaction methods: `begin_committed_frame(...) -> bool`, `active_models(configuration_revision) -> tuple[JdmModel, ...]`, `save_execution(execution) -> None`
- Produces: `build_postgres_committed_l2_jdm_consumer() -> CommittedL2JdmConsumer`

- [ ] **Step 1: Write real PostgreSQL atomicity tests**

```python
def test_receipt_and_all_model_executions_commit_together(self):
    consumer.publish_sync(self.frame_with_two_inputs())
    self.assert_db_count("t_committed_frame_consumers", "consumer_key='jdm'", 1)
    self.assert_db_count("t_jdm_executions", "frame_id=%s", 2)

def test_second_execution_failure_rolls_back_receipt_and_first_execution(self):
    self.install_execution_failure_trigger(second_rule_id)
    with self.assertRaises(Exception):
        consumer.publish_sync(self.frame_with_two_models())
    self.assert_db_count("t_committed_frame_consumers", "consumer_key='jdm'", 0)
    self.assert_db_count("t_jdm_executions", "frame_id=%s", 0)
```

Also prove configuration mismatch rolls back, same frame replay is a no-op, and same sequence with another frame fails the unique constraint.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest backend.tests.test_committed_l2_jdm_postgres.JdmRuntimePostgresTest -v`

Expected: import failure for `PostgresJdmRepository`.

- [ ] **Step 3: Implement one PostgreSQL transaction adapter**

`begin_committed_frame` must first lock/read `t_configuration_state.current_revision`, require exact equality with the event revision, then insert into `t_committed_frame_consumers` using consumer key `jdm`. `active_models` selects only enabled `control`/`linkage` rows whose `configuration_revision <= event.configuration_revision`. `save_execution` is insert-only and uses `psycopg2.extras.Json` for inputs/outputs/actions.

- [ ] **Step 4: Run GREEN and commit**

Run: `python -m unittest backend.tests.test_committed_l2_jdm_consumer backend.tests.test_committed_l2_jdm_postgres.JdmRuntimePostgresTest -v`

```bash
git add backend/app/services/jdm_postgres.py backend/app/services/committed_l2_jdm_consumer.py backend/tests/test_committed_l2_jdm_postgres.py
git commit -m "feat(jdm): persist atomic frame executions"
```

### Task 4: 配置栅栏、同一试运行 adapter 与旧旁路硬切

**Files:**
- Modify: `backend/app/api/rules.py`
- Delete: `backend/app/services/rule_engine.py`
- Delete: `backend/app/services/rule_alarm_adapter.py`
- Delete: `backend/tests/test_rule_alarm_adapter_contract.py`
- Modify: `backend/tests/test_jdm_configuration_revision_public_api.py`
- Modify: `backend/tests/test_data_trunk_startup_gate.py`

**Interfaces:**
- Produces: every create/update/delete writes the resulting `configuration_revision` to the current `t_rules` row in the same transaction.
- Produces: `GET /api/v1/rules/{rule_id}/executions?limit=50` read-only response.
- Produces: `/rules/evaluate` and `/rules/{id}/simulate` call `jdm_runtime.evaluate_model_content`; neither persists actions.

- [ ] **Step 1: Write API RED tests**

```python
def test_jdm_update_drains_runtime_and_binds_new_configuration_revision(self):
    gate = RecordingRuntimeGate(current_revision=7)
    response = self.client.put(f"/api/v1/rules/{rule_id}", json={"jdm_content": content_v2})
    self.assertEqual(200, response.status_code)
    self.assertEqual([("begin", 7), ("reconcile", 8)], gate.calls)
    self.assertEqual(8, response.json()["configuration_revision"])

def test_simulation_uses_same_adapter_but_creates_no_execution_or_receipt(self):
    before = self.execution_counts()
    response = self.client.post(f"/api/v1/rules/{rule_id}/simulate", json={"context": {"power": 12}})
    self.assertEqual(200, response.status_code)
    self.assertEqual(before, self.execution_counts())
```

Also assert drain timeout is 409/503 with zero rule write, create/update reject `alarm` and `fault_map`, and sourceNodeIds/sourceEntityIds/L0-like mappings return a stable 409 code.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest backend.tests.test_jdm_configuration_revision_public_api backend.tests.test_data_trunk_startup_gate -v`

- [ ] **Step 3: Wrap every JDM configuration mutation in the existing gate**

```python
async def _publish_jdm_change(runtime, base_revision: int, operation):
    gate = runtime.data_trunk.configuration_gate
    await asyncio.to_thread(gate.begin_configuration_publish, base_revision)
    try:
        result = await asyncio.to_thread(operation, base_revision)
    except Exception:
        gate.cancel_configuration_publish()
        raise
    await asyncio.to_thread(gate.reconcile_configuration_runtime)
    return result
```

The SQL operation must lock the configuration state, mutate `t_rules`, publish the revision, update the row's `configuration_revision`, replace entity references, and commit once. Delete publishes its audit revision before deleting the row. HTTP must expose stable error codes rather than raw exception strings.

- [ ] **Step 4: Hard-delete the latest/L0 and direct-action runtime**

Delete `run_rule_tick`, `_build_context`, L0/tag/entity legacy context, direct alarm submission and direct control command creation. New JDM models accept only `rule_type in ('control','linkage')`, `_config.sourceEntityInstanceIds`, `_config.inputMappings`, and L2 entity UUIDs. Historical unsupported rule rows remain listable but cannot be enabled or updated without migration.

- [ ] **Step 5: Unify zero-side-effect simulation and add execution reads**

Expose the pure evaluator through `evaluate_model_content(content, context)` and keep the editor response wrapper expected by the frontend. The execution endpoint selects by `rule_id`, orders by `frame_sequence DESC`, clamps limit to 1–100, and returns no physical route, MQTT payload, credential or raw L0 field.

- [ ] **Step 6: Run GREEN and commit**

Run: `python -m unittest backend.tests.test_jdm_configuration_revision_public_api backend.tests.test_data_trunk_startup_gate -v`

```bash
git add backend/app/api/rules.py backend/app/services/jdm_runtime.py backend/tests/test_jdm_configuration_revision_public_api.py backend/tests/test_data_trunk_startup_gate.py
git rm backend/app/services/rule_engine.py backend/app/services/rule_alarm_adapter.py backend/tests/test_rule_alarm_adapter_contract.py
git commit -m "refactor(jdm): remove latest and l0 execution paths"
```

### Task 5: 生产 fanout 与最小执行可见性

**Files:**
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_data_frame_outbox.py`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/pages/RuleEnginePage.tsx`
- Test: `frontend/src/components/rule-engine/jdmExecutionModel.test.mjs`

**Interfaces:**
- Startup order: `alarm → jdm → committed_frame_stream`
- Frontend type: `JdmExecutionSummary { frame_sequence, configuration_revision, status, reason_code, executed_at, outputs }`

- [ ] **Step 1: Write fanout order and UI model RED tests**

```python
self.assertEqual(["alarm", "jdm", "stream"], calls)
```

```javascript
assert.equal(jdmExecutionLabel({status: 'rejected', reason_code: 'JDM_INPUT_QUALITY_NOT_GOOD'}), '已拒绝：输入质量不可用')
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest backend.tests.test_data_frame_outbox -v`

Run: `node --test frontend/src/components/rule-engine/jdmExecutionModel.test.mjs`

- [ ] **Step 3: Register the JDM consumer and display recent facts**

```python
CommittedFrameFanout((
    build_postgres_committed_l2_alarm_consumer(),
    build_postgres_committed_l2_jdm_consumer(),
    committed_frame_stream,
))
```

The rule list/detail may request recent facts but must not trigger evaluation. Show frame sequence, execution time, executed/rejected, and translated reason; keep raw inputs/actions under an existing technical-details disclosure rather than the first screen.

- [ ] **Step 4: Run GREEN and commit**

Run: `python -m unittest backend.tests.test_data_frame_outbox backend.tests.test_data_trunk_startup_gate -v`

Run: `node --test frontend/src/components/rule-engine/*.test.mjs`

```bash
git add backend/app/main.py backend/tests/test_data_frame_outbox.py frontend/src/api/client.ts frontend/src/pages/RuleEnginePage.tsx frontend/src/components/rule-engine
git commit -m "feat(jdm): expose committed execution status"
```

### Task 6: Complete verification, release, deploy and safe Browser acceptance

**Files:**
- Modify: `VERSION`
- Modify: `backend/app/VERSION`
- Modify: `backend/pyproject.toml`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Create: `docs/deploy-1号机-v0.4.91-http.md`
- Modify: `CODEX_HANDOFF.md`

**Interfaces:**
- Release: `0.4.91`, Schema `052`, immutable linux/arm64 digest.

- [ ] **Step 1: Run focused real-PostgreSQL gates**

Run the Schema 052, JDM transaction, API revision, outbox and startup tests against an isolated database whose name ends in `_test`. Required result: zero failures and zero unexpected skips.

- [ ] **Step 2: Run all repository gates**

Run backend full tests, scripts full tests, frontend all `*.test.mjs`, TypeScript, Vite production build, `compileall`, version consistency and `git diff --check`. Record exact counts; do not reuse earlier release counts.

- [ ] **Step 3: Build and publish once**

Bump all five version files to `0.4.91`, commit, tag, push, dispatch `release-images.yml`, and read the ARM64 digest from the completed `release.json`. Do not build on 1 号机 and do not use `latest`.

- [ ] **Step 4: Protect and deploy 1 号机**

Create a custom-format PostgreSQL backup, verify SHA-256 and `pg_restore -l`, then only recreate backend with the fixed ARM64 digest. Preserve runtime env, volume, host networking, `/dev/mqueue`, HTTP mode and the v0.4.90 rollback image.

- [ ] **Step 5: Browser acceptance without executing a strategy**

Use Browser to verify node tree, one fresh and one STALE L0, L1 trial only, L2 current/history/source, alarm pages, and the JDM page. For JDM, run only explicit zero-side-effect simulation and read existing execution history; do not enable a rule, submit a control command, execute an automatic strategy or write a device.

- [ ] **Step 6: Record evidence**

Document container image ID, health/restart count, Schema 052, outbox pending count, `jdm` consumer receipt progress, JDM execution count/status, log error scan, disk, backup evidence and Browser results. Keep the platform goal active because automatic control and EMS committed-stream convergence remain later slices.
