# Committed L2 告警消费者实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让统一告警状态机只从已提交数据帧的 L2 变化运行，并使重放、崩溃和配置切换保持确定。

**Architecture:** 保留 `FrameOutboxDispatcher` 为唯一有序队头，在其后加入串行 `CommittedFrameFanout`。
告警消费者把一个终态帧的全部 L2 告警观察放进同一 PostgreSQL 事务，并在该事务内写通用消费收据；
告警配置应用复用现有 `ConfigurationRuntimeGate`。旧 L0/MQTT/latest 告警入口硬删除。

**Tech Stack:** Python 3.12、FastAPI、PostgreSQL/TimescaleDB、`unittest`、现有手写 SQL；不增加依赖。

**Spec:** `docs/superpowers/specs/2026-08-28-upper-layer-committed-l2-convergence-design.md`

## Global Constraints

- 上层告警只消费事务 B 已提交的 `FrameOutboxEvent.l2_changes`，不得读取 L0、MQTT 或 latest。
- 帧分发保持 ordered at-least-once；状态变化通过事务内消费收据做到副作用一次。
- 非 GOOD 值不得触发或恢复告警；不得用当前时间填补缺失的 L2 `observed_at`。
- 配置发布必须排空旧帧与未发布 outbox，并在成功后让黑板进入新修订 WARMING。
- 不新增 Redis、Kafka、微服务、依赖或第二条 outbox。
- 不执行真实设备写入，不启用 Caddy/TLS；1 号机保持 host 网络与 `/dev/mqueue`。

---

### Task 1: 串行 committed-frame fanout

**Files:**
- Modify: `backend/app/services/data_trunk_outbox.py`
- Modify: `backend/tests/test_data_frame_outbox.py`

**Interfaces:**
- Consumes: 现有 `CommittedFramePublisher.publish(event: FrameOutboxEvent) -> None`
- Produces: `CommittedFrameFanout(consumers: tuple[CommittedFramePublisher, ...])`

- [ ] **Step 1: 写 fanout 顺序和失败测试**

```python
async def test_fanout_delivers_one_frame_to_consumers_in_registration_order(self):
    calls = []
    first = _CallbackPublisher(lambda event: calls.append(("alarm", event.frame_sequence)))
    second = _CallbackPublisher(lambda event: calls.append(("stream", event.frame_sequence)))
    await CommittedFrameFanout((first, second)).publish(_event(10))
    self.assertEqual([("alarm", 10), ("stream", 10)], calls)

async def test_fanout_failure_stops_later_consumers_and_leaves_outbox_unpublished(self):
    event = _event(10)
    repository = InMemoryFrameOutboxRepository((event,), clock=lambda: NOW)
    calls = []
    fanout = CommittedFrameFanout((
        _FailingPublisher(calls, "alarm"),
        _CallbackPublisher(lambda value: calls.append(("stream", value.frame_sequence))),
    ))
    self.assertEqual(0, await FrameOutboxDispatcher(repository, fanout).run_once(now=NOW))
    self.assertEqual(["alarm"], calls)
    self.assertEqual((), repository.published_ids)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `cd backend; .venv/Scripts/python.exe -m unittest tests.test_data_frame_outbox -v`

Expected: FAIL，`CommittedFrameFanout` 尚不存在。

- [ ] **Step 3: 写最小 fanout**

```python
class CommittedFrameFanout:
    def __init__(self, consumers: tuple[CommittedFramePublisher, ...]) -> None:
        if not consumers:
            raise ValueError("committed frame fanout requires a consumer")
        self._consumers = consumers

    async def publish(self, event: FrameOutboxEvent) -> None:
        for consumer in self._consumers:
            await consumer.publish(event)
```

- [ ] **Step 4: 运行测试并确认 GREEN**

Run: `cd backend; .venv/Scripts/python.exe -m unittest tests.test_data_frame_outbox -v`

Expected: PASS；第一个消费者失败时第二个未调用，outbox 未发布。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/data_trunk_outbox.py backend/tests/test_data_frame_outbox.py
git commit -m "feat: fan out committed data frames"
```

### Task 2: 告警状态机的帧级原子批处理

**Files:**
- Modify: `backend/app/services/alarm_runtime.py`
- Modify: `backend/app/services/alarm_definition_dispatch.py`
- Modify: `backend/app/services/alarm_postgres.py`
- Create: `backend/app/services/committed_l2_alarm_consumer.py`
- Create: `backend/tests/test_committed_l2_alarm_consumer.py`
- Modify: `backend/tests/test_alarm_runtime.py`

**Interfaces:**
- Consumes: `FrameOutboxEvent`、`AlarmDefinition`、`AlarmObservation`
- Produces: `AlarmEvaluation`、`AlarmRuntime.submit_frame(...)`、`CommittedL2AlarmConsumer.publish(...)`
- Produces: `AlarmDefinitionCatalog.all_versions()` 和 `AlarmDefinitionDispatcher.for_entities(...)`

- [ ] **Step 1: 写失败测试，证明只读 L2、记录完整帧证据**

```python
async def test_committed_consumer_submits_only_l2_with_frame_evidence(self):
    consumer, repository = _consumer(_definition(trigger={"op": "gt", "value": 10}))
    event = _frame(l0_value=999, l2_value=11, quality=TrunkQuality.GOOD)
    await consumer.publish(event)
    active = repository.active_events()[0]
    evidence = active.last_observation["evidence"]
    self.assertEqual(str(event.frame_id), evidence["frame_id"])
    self.assertEqual(event.frame_sequence, evidence["frame_sequence"])
    self.assertEqual(event.configuration_revision, evidence["configuration_revision"])
    self.assertEqual("committed_l2", active.last_observation["source_kind"])
```

- [ ] **Step 2: 写失败测试，证明同帧重放和事务回滚**

```python
async def test_same_frame_is_a_noop_after_atomic_alarm_commit(self):
    consumer, repository = _consumer(_overlapping_zero_duration_definition())
    event = _frame(l2_value=1, quality=TrunkQuality.GOOD)
    await consumer.publish(event)
    first_events = repository.list_events()
    first_transitions = repository.transitions(first_events[0].id)
    await consumer.publish(event)
    self.assertEqual(first_events, repository.list_events())
    self.assertEqual(first_transitions, repository.transitions(first_events[0].id))

async def test_invalid_second_observation_rolls_back_first_and_receipt(self):
    consumer, repository = _consumer_for_two_entities()
    bad = _frame_with_second_observed_at_missing()
    with self.assertRaisesRegex(AlarmRuntimeError, "ALARM_FRAME_OBSERVATION_INVALID"):
        await consumer.publish(bad)
    self.assertEqual((), repository.list_events())
    self.assertFalse(repository.has_consumed_frame("alarm", bad.frame_id))
```

- [ ] **Step 3: 运行测试并确认 RED**

Run: `cd backend; .venv/Scripts/python.exe -m unittest tests.test_committed_l2_alarm_consumer tests.test_alarm_runtime -v`

Expected: FAIL，帧消费者、批处理入口和消费收据尚不存在。

- [ ] **Step 4: 增加帧评估与批处理入口**

```python
@dataclass(frozen=True)
class AlarmEvaluation:
    definition: AlarmDefinition
    observation: AlarmObservation

def submit_frame(
    self,
    *,
    frame_id: UUID,
    frame_sequence: int,
    configuration_revision: int,
    evaluations: tuple[AlarmEvaluation, ...],
) -> tuple[AlarmOutcome, ...]:
    with self._repository.transaction() as repository:
        if not repository.begin_committed_frame(
            "alarm", frame_id, frame_sequence, configuration_revision
        ):
            return ()
        return tuple(
            self._submit_with_definition(repository, item.definition, item.observation)
            for item in evaluations
        )
```

把现有 `submit` 的状态机主体提取为 `_submit_with_definition`；单条 `submit` 仍先从 catalog 取定义并打开
一次事务。`InMemoryAlarmRepository.transaction()` 在异常时恢复事件、转换、通知和收据快照。

- [ ] **Step 5: 增加批量定义分发**

```python
def for_entities(self, entity_ids: frozenset[UUID]) -> dict[UUID, tuple[AlarmDefinition, ...]]:
    current = tuple(item for item in self._definitions.all_definitions()
                    if item.entity_instance_id in entity_ids)
    versions = {item.id: item for item in self._definitions.all_versions()}
    open_events = tuple(item for item in self._alarm_runtime.list()
                        if item.entity_instance_id in entity_ids and item.state in OPEN_STATES)
    return _merge_current_and_open_versions(current, versions, open_events, entity_ids)
```

PostgreSQL 的 `all_versions()` 用一条不连接 `t_alarm_definition_current` 的查询返回全部不可变定义；内存
catalog 返回 `_definitions.values()`。

- [ ] **Step 6: 写 committed L2 告警消费者**

```python
class CommittedL2AlarmConsumer:
    async def publish(self, event: FrameOutboxEvent) -> None:
        await asyncio.to_thread(self._consume, event)

    def _consume(self, event: FrameOutboxEvent) -> None:
        grouped = self._dispatcher.for_entities(
            frozenset(item.entity_instance_id for item in event.l2_changes)
        )
        evaluations = tuple(
            _evaluation(event, change, definition)
            for change in event.l2_changes
            for definition in grouped.get(change.entity_instance_id, ())
        )
        self._runtime.submit_frame(
            frame_id=event.frame_id,
            frame_sequence=event.frame_sequence,
            configuration_revision=event.configuration_revision,
            evaluations=evaluations,
        )
```

`_evaluation` 要求 `change.observed_at` 非空，quality 使用 `int(change.quality)`，并写入规格列出的全部来源
证据；不读取 L0，也不查询实体 latest。

- [ ] **Step 7: 运行测试并确认 GREEN**

Run: `cd backend; .venv/Scripts/python.exe -m unittest tests.test_committed_l2_alarm_consumer tests.test_alarm_runtime tests.test_alarm_event_public_api -v`

Expected: PASS；同帧重放不产生第二次转换，异常帧不留部分事件或收据。

- [ ] **Step 8: 提交**

```bash
git add backend/app/services/alarm_runtime.py backend/app/services/alarm_definition_dispatch.py backend/app/services/alarm_postgres.py backend/app/services/committed_l2_alarm_consumer.py backend/tests/test_committed_l2_alarm_consumer.py backend/tests/test_alarm_runtime.py
git commit -m "feat: consume alarm observations by committed frame"
```

### Task 3: Schema 049 通用消费收据

**Files:**
- Create: `init-db/migration_049_committed_frame_consumers.sql`
- Create: `backend/tests/test_committed_frame_consumers_migration_postgres.py`
- Modify: `backend/app/services/alarm_postgres.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/tests/test_data_trunk_startup_gate.py`

**Interfaces:**
- Consumes: `_PostgresAlarmTransaction` 的现有业务事务
- Produces: `begin_committed_frame(consumer_key, frame_id, frame_sequence, configuration_revision) -> bool`

- [ ] **Step 1: 写迁移失败测试**

```python
def test_049_installs_generic_frame_consumer_receipts(self):
    with self._connection() as connection, connection.cursor() as cursor:
        self._apply_through_048(cursor)
        cursor.execute(MIGRATION_049.read_text(encoding="utf-8"))
        cursor.execute("SELECT consumer_key,frame_id,frame_sequence,configuration_revision "
                       "FROM t_committed_frame_consumer_receipts")
        self.assertEqual([], cursor.fetchall())
        self.assertTrue(_has_cascade_frame_fk(cursor))

def test_receipt_identity_rejects_duplicate_frame_and_sequence_per_consumer(self):
    with self._connection() as connection, connection.cursor() as cursor:
        self._apply_through_049(cursor)
        frame_id = self._insert_complete_frame(cursor, sequence=1)
        cursor.execute(
            "INSERT INTO t_committed_frame_consumer_receipts "
            "(consumer_key,frame_id,frame_sequence,configuration_revision) "
            "VALUES('alarm',%s,1,0)",
            (frame_id,),
        )
        cursor.execute("SAVEPOINT duplicate_receipt")
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            cursor.execute(
                "INSERT INTO t_committed_frame_consumer_receipts "
                "(consumer_key,frame_id,frame_sequence,configuration_revision) "
                "VALUES('alarm',%s,1,0)",
                (frame_id,),
            )
        cursor.execute("ROLLBACK TO SAVEPOINT duplicate_receipt")
        cursor.execute(
            "INSERT INTO t_committed_frame_consumer_receipts "
            "(consumer_key,frame_id,frame_sequence,configuration_revision) "
            "VALUES('jdm',%s,1,0)",
            (frame_id,),
        )
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `cd backend; .venv/Scripts/python.exe -m unittest tests.test_committed_frame_consumers_migration_postgres -v`

Expected: 在显式 PostgreSQL 测试环境中 FAIL，因为 migration 049 不存在；未配置环境时显示 skip，随后在
1 号机隔离恢复库执行同一测试。

- [ ] **Step 3: 写幂等迁移**

```sql
CREATE TABLE public.t_committed_frame_consumer_receipts (
  consumer_key TEXT NOT NULL CHECK (consumer_key IN ('alarm','jdm','automatic_control')),
  frame_id UUID NOT NULL REFERENCES public.t_data_frames(frame_id) ON DELETE CASCADE,
  frame_sequence BIGINT NOT NULL CHECK (frame_sequence > 0),
  configuration_revision BIGINT NOT NULL CHECK (configuration_revision >= 0),
  consumed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (consumer_key,frame_id),
  UNIQUE (consumer_key,frame_sequence)
);
```

迁移先取得 `zizu-schema-049` advisory lock，要求 Schema 048 对象存在；完整结构已存在则返回，部分结构
存在则报 `SCHEMA_049_PARTIAL_STRUCTURE`。

- [ ] **Step 4: 在 PostgreSQL 告警事务内写收据并校验活动修订**

```python
def begin_committed_frame(self, consumer_key, frame_id, frame_sequence, configuration_revision):
    with self._connection.cursor() as cur:
        cur.execute("SELECT current_revision FROM t_configuration_state WHERE singleton=TRUE FOR SHARE")
        if int(cur.fetchone()[0]) != configuration_revision:
            raise AlarmRuntimeError(
                "ALARM_FRAME_CONFIGURATION_MISMATCH",
                "Committed alarm frame revision is not active",
            )
        cur.execute(
            "INSERT INTO t_committed_frame_consumer_receipts "
            "(consumer_key,frame_id,frame_sequence,configuration_revision) "
            "VALUES(%s,%s,%s,%s) "
            "ON CONFLICT (consumer_key,frame_id) DO NOTHING RETURNING frame_id",
            (consumer_key, frame_id, frame_sequence, configuration_revision),
        )
        return cur.fetchone() is not None
```

- [ ] **Step 5: 把收据表加入生产启动契约**

`verify_data_trunk_contract_gate()` 必须检查表、主键、唯一索引和级联外键；测试断言启动 SQL 包含
`t_committed_frame_consumer_receipts`。

- [ ] **Step 6: 运行测试并确认 GREEN**

Run: `cd backend; .venv/Scripts/python.exe -m unittest tests.test_committed_frame_consumers_migration_postgres tests.test_data_trunk_startup_gate tests.test_committed_l2_alarm_consumer -v`

Expected: 单元测试 PASS；显式 PostgreSQL 环境下迁移与行为 PASS。

- [ ] **Step 7: 提交**

```bash
git add init-db/migration_049_committed_frame_consumers.sql backend/app/services/alarm_postgres.py backend/app/services/data_trunk_postgres.py backend/tests/test_committed_frame_consumers_migration_postgres.py backend/tests/test_data_trunk_startup_gate.py
git commit -m "feat: persist committed frame consumer receipts"
```

### Task 4: 告警配置栅栏与生产接线

**Files:**
- Modify: `backend/app/services/alarm_configuration.py`
- Modify: `backend/app/services/alarm_configuration_postgres.py`
- Modify: `backend/app/api/alarm_configurations.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_alarm_configuration_l2.py`
- Modify: `backend/tests/test_data_trunk_startup_gate.py`

**Interfaces:**
- Consumes: `ConfigurationRuntimeGate`、`CommittedL2AlarmConsumer`、`CommittedFrameFanout`
- Produces: `build_postgres_alarm_configuration()`、`build_postgres_committed_l2_alarm_consumer()`

- [ ] **Step 1: 写配置栅栏失败测试**

```python
def test_alarm_apply_drains_frames_then_reconciles_new_revision(self):
    gate = _RecordingGate()
    service = AlarmConfiguration(_ApplyRepository(result_revision=8), runtime_gate=gate)
    result = service.apply(_command())
    self.assertEqual(8, result.configuration_revision)
    self.assertEqual([("begin", 7), ("reconcile", 8)], gate.calls)

def test_alarm_apply_cancels_gate_when_repository_fails(self):
    gate = _RecordingGate()
    with self.assertRaises(AlarmConfigurationError):
        AlarmConfiguration(_FailingApplyRepository(), runtime_gate=gate).apply(_command())
    self.assertEqual([("begin", 7), ("cancel", None)], gate.calls)
```

- [ ] **Step 2: 写生产接线失败测试**

```python
def test_main_registers_alarm_before_live_frame_stream(self):
    source = inspect.getsource(main.lifespan)
    self.assertIn("CommittedFrameFanout", source)
    self.assertLess(source.index("committed_l2_alarm_consumer"),
                    source.index("committed_frame_stream"))
```

- [ ] **Step 3: 运行测试并确认 RED**

Run: `cd backend; .venv/Scripts/python.exe -m unittest tests.test_alarm_configuration_l2 tests.test_data_trunk_startup_gate -v`

Expected: FAIL，告警配置没有 runtime gate，main 仍把 stream 直接交给 dispatcher。

- [ ] **Step 4: 复用现有配置发布栅栏**

`AlarmConfiguration.__init__` 接受可选 `runtime_gate`。`apply` 先读取 plan 的基础修订，调用
`begin_configuration_publish`；repository 失败时 `cancel_configuration_publish`；成功后调用
`reconcile_configuration_runtime`。`DataTrunkError.code` 映射为同名 `AlarmConfigurationError`。

`build_postgres_alarm_configuration()` 和点位加工 builder 一样，从 `app.main.get_pipeline()` 获取当前
`pipeline.data_trunk.configuration_gate`；API dependency 只调用 builder。

- [ ] **Step 5: 接线告警消费者和实时流**

```python
committed_l2_alarm_consumer = build_postgres_committed_l2_alarm_consumer()
publisher = CommittedFrameFanout((
    committed_l2_alarm_consumer,
    committed_frame_stream,
))
frame_outbox_dispatcher = FrameOutboxDispatcher(
    PostgresFrameOutboxRepository(), publisher
)
```

告警先于浏览器流处理；告警异常使该帧保持未发布，浏览器不会看到“业务消费者尚未接受”的游标。

- [ ] **Step 6: 运行测试并确认 GREEN**

Run: `cd backend; .venv/Scripts/python.exe -m unittest tests.test_alarm_configuration_l2 tests.test_data_trunk_startup_gate tests.test_data_frame_outbox tests.test_committed_l2_alarm_consumer -v`

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/alarm_configuration.py backend/app/services/alarm_configuration_postgres.py backend/app/api/alarm_configurations.py backend/app/main.py backend/tests/test_alarm_configuration_l2.py backend/tests/test_data_trunk_startup_gate.py
git commit -m "feat: fence alarm configuration on committed frames"
```

### Task 5: 硬切旧入口、验证和单次发布

**Files:**
- Delete: `backend/app/services/tag_mqtt_alarm_adapter.py`
- Delete: `backend/app/services/entity_alarm_adapter.py`
- Delete: `backend/app/services/alarm_processor.py`
- Delete: `backend/app/services/tag_alarm_engine.py`
- Delete: `backend/tests/test_entity_alarm_adapter_contract.py`
- Modify: `backend/tests/test_entity_instance_l2_runtime.py`
- Modify: `backend/tests/test_rule_alarm_adapter_contract.py`
- Modify: `backend/app/services/alarm_logic.py`
- Modify: `docs/adr/0004-unified-alarm-state-machine-interface.md`
- Modify: `VERSION`
- Modify: `backend/app/VERSION`
- Modify: `backend/pyproject.toml`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `README.md`

**Interfaces:**
- Removes: L0/MQTT/latest 告警运行入口与兼容空壳
- Produces: `v0.4.85-rc.10` / Schema 049 候选制品

- [ ] **Step 1: 把旧适配器契约改写为 committed-frame 契约**

`test_entity_instance_l2_runtime.py` 保留实体 runtime 的读取契约，但删除 `EntityAlarmAdapter` 场景；其告警
覆盖由 `test_committed_l2_alarm_consumer.py` 承担。`test_rule_alarm_adapter_contract.py` 不再要求四个旧模块
路径存在，改为断言生产 pipeline 不导入这些模块且只忽略旧告警 topic。

- [ ] **Step 2: 运行测试并确认旧模块仍使测试 RED**

Run: `cd backend; .venv/Scripts/python.exe -m unittest tests.test_entity_instance_l2_runtime tests.test_rule_alarm_adapter_contract tests.test_committed_l2_alarm_consumer -v`

Expected: FAIL，源码扫描仍能找到旧入口。

- [ ] **Step 3: 删除旧模块并更新 ADR**

删除列出的四个生产文件和一个旧契约测试；`alarm_logic.py` 的说明不再引用旧模块；ADR-0004 增补说明
其“Tag/MQTT 统一适配器”决策已由 ADR-0015 的 committed L2 正式入口取代。不得保留 import shim、
空壳或运行期 fallback。

- [ ] **Step 4: 更新候选版本**

只把 `VERSION`、`backend/app/VERSION`、`backend/pyproject.toml`、`frontend/package.json`、
`frontend/package-lock.json` 和根 README 的发布版本从 `0.4.85-rc.9` 改为 `0.4.85-rc.10`；规格中的
“rc.9 基线”保持不变。README 当前状态说明 Schema 049、告警 committed L2 消费者和配置栅栏，不宣称
JDM/控制/工作台已收口。

- [ ] **Step 5: 运行相关与完整验证**

Run:

```powershell
Set-Location backend
& '.\.venv\Scripts\python.exe' -m unittest tests.test_data_frame_outbox tests.test_committed_l2_alarm_consumer tests.test_alarm_runtime tests.test_alarm_event_public_api tests.test_alarm_configuration_l2 tests.test_rule_alarm_adapter_contract tests.test_data_trunk_startup_gate -v
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
Set-Location ..
& '.\backend\.venv\Scripts\python.exe' -m unittest scripts.test_validate_release_rollback scripts.test_release_workflow scripts.test_release_preflight scripts.test_release_image_build scripts.test_release_entrypoint scripts.test_release_compose scripts.test_record_release_lock scripts.test_build_release_images -v
git diff --check
```

Expected: 所有非显式环境测试 PASS；PostgreSQL 测试只在 `ZIZU_POSTGRES_TEST=1` 时运行。

- [ ] **Step 6: 提交、推送并只构建一次最终候选**

```bash
git add -A
git commit -m "feat: hard cut alarms to committed L2"
git push origin ticket/v0.4.85-node-data-trunk-hard-cut
git tag v0.4.85-rc.10
git push origin v0.4.85-rc.10
```

等待 GitHub Actions 成功并记录 ARM64 固定摘要；不移动既有标签，不使用 `latest`。

- [ ] **Step 7: 部署与只读验收 1 号机**

1. 清理磁盘前只删除已确认可重建的旧镜像层，不删除数据库卷或备份；
2. 先做并验证完整数据库备份与隔离恢复；
3. 由 owner 应用 Schema 049，再执行应用角色授权脚本；
4. 以 ARM64 固定摘要、`network_mode: host`、`/dev/mqueue` tmpfs 替换测试后端；
5. 验证容器 running/healthy、restart=0、版本 rc.10、Schema 049；
6. 用现有电表 L2 或模拟 committed frame 验证：GOOD 触发、STALE 不恢复、同帧重放无重复转换、配置
   栅栏排空后才发布；
7. 不写设备、不执行自动控制、不启用 Caddy/TLS。

- [ ] **Step 8: 更新交接记录**

在工作区 `CODEX_HANDOFF.md` 顶部记录提交、测试、镜像摘要、备份、Schema、容器状态、告警验收证据和
下一切片“JDM committed L2 消费者”。
