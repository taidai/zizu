# 单站数据帧底座实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有“MQTT 缓冲批次内一次事务写完 L0/L2”硬切为“单站实时黑板 → 事务 A 保住变化 L0 → 固定配置修订运行完整 L1 → 事务 B 原子提交终态 L0/L2 与统一帧 outbox”的可恢复秒级底座。

**Architecture:** 保留 `DataTrunk` 作为唯一外部写入模块，把复杂度收进三个深模块：`RealtimeBlackboard` 负责接收、判新旧、READY/STALE 和冻结；`FrameProcessor` 负责领取帧、固定修订全量 L1、恢复与失败闭环；PostgreSQL `FrameRepository` adapter 负责事务 A/B、租约、latest 序号和来源证据。当前是一套数据库对应一个场站，因此 `frame_sequence` 在数据库内全局唯一，不新建 `t_sites` 或虚构 `site_id`。

**Tech Stack:** Python 3.12、FastAPI lifespan、`unittest`、psycopg2、PostgreSQL 16、TimescaleDB 2.x、asyncio、现有 NanoMQ/Neuron MQTT 入口。

**Spec:** `docs/superpowers/specs/2026-08-27-site-realtime-blackboard-frame-design.md`；总体约束见 `docs/superpowers/specs/2026-08-27-zizu-platform-core-architecture-design.md` 与 `docs/adr/0014-site-realtime-blackboard-and-committed-frames.md`。

## Global Constraints

- 本阶段固定单站、单活动采集写者、`snapshot_interval_ms=1000`、连续 3 拍未更新转 `STALE`、最多 3 次处理尝试且帧总年龄不超过 60 秒。
- 迟到、乱序和重复样本直接丢弃；同一点位同一拍只保留最后一条严格更新的样本。
- `STALE` 保留最后已知值，只改变质量；`BAD` 可按既有契约保留空值。机器消费者仍须按质量 fail closed。
- 事务 A 只写 `PENDING` 帧及本拍变化 L0；事务 B 才公开推进 L0 latest、写全量即时 L2、来源、终态帧 outbox 并置为 `COMPLETE`。
- `FAILED` 终结也必须在一个事务内推进已提交 L0 latest、写受影响 L2 的 `STALE` 终态、写一条帧 outbox 并结束帧。
- 每个冻结候选在进入数据库前已有稳定 `frame_id + candidate_digest + capture_beat`；事务 A 的结果未知重试只能返回原帧，绝不能再造第二帧。
- `PROCESSING` 帧和帧 outbox 的领取都使用 owner + 一次性 token 做 fencing；过期 worker 的晚到提交必须零写拒绝。
- 新主干 latest 只按 `frame_sequence` 推进；业务时间保留用于显示、追溯和统计，不参与终态先后判断。
- Schema 046 一次硬切旧 `t_l2_stream_outbox`，不得双写、兼容表、运行期 fallback 或第二套帧顺序。
- 不引入 Redis、Kafka、新微服务、新依赖、逐点节拍、巨型帧 JSON、增量规则计算或 active-active。
- 本计划不实现 REST 快照/游标、公开 WebSocket 帧协议和前端页面；它们属于第二阶段实时界面计划。
- 本计划不把告警、JDM、控制和工作台接到 committed L2；它们属于第三阶段上层收口计划。
- 本计划不构建候选镜像、不打版本标签、不合入可发布分支，也不部署 1 号机。第二阶段实时界面和第三阶段上层收口同样不得单独发布；只有第四阶段 EMS 纵向验收完成，且即时 L2/来源/帧表具有已确认的有界保留策略后，才可另写、另确认发布部署计划。
- 测试沿用 `unittest`，不添加 pytest；真实 PostgreSQL 测试必须设置 `ZIZU_POSTGRES_TEST=1`，且 `DB_NAME` 必须以 `_test` 结尾。
- Schema 045 只约束了 L0 容量；新帧会把 L2/来源/帧元数据变为高增长对象。本阶段记录真实行数与字节/帧，但不擅自决定 L2 历史保留期；生产发布前必须先确认并实现有界窗口，禁止以“以后再清理”为由上线。

---

## 文件结构与职责

### 新建

- `backend/app/services/realtime_blackboard.py`：纯内存黑板；只有 `accept_many()`、`tick()`、`acknowledge()`、`reset_revision()` 四个运行接口。
- `backend/app/services/frame_processor.py`：`FrameRepository` seam、`FrameProcessor.process_next()` 和 DAG 失败闭包；不处理 MQTT 或 WebSocket。
- `init-db/migration_046_data_frames.sql`：帧表、L0/L2 帧列、latest 序号、统一帧 outbox 和硬切约束。
- `backend/tests/test_realtime_blackboard.py`：黑板纯行为测试。
- `backend/tests/test_data_frames_migration_postgres.py`：Schema 046 新装、升级、重放和损坏拒绝测试。
- `backend/tests/run_postgres_group.py`：显式 PostgreSQL 测试组的环境/`*_test` 防误连门禁，并把任何 skip 视为失败。
- `backend/tests/test_frame_processor.py`：正常 DAG、恢复、重试和失败闭包的 in-memory adapter 测试。
- `backend/tests/test_data_frames_postgres.py`：真实 PostgreSQL 事务 A/B、租约、latest、防倒退和 outbox 测试。
- `backend/tests/test_data_frame_outbox.py`：帧 outbox 领取、退避、幂等和发布对象测试。
- `backend/tests/test_data_frame_acceptance_postgres.py`：从 canonical L0 到终态帧的一条机器可核对主缝。

### 修改

- `backend/app/services/data_trunk_contracts.py:38-352`：增加结构化源顺序、帧、拍号、冻结视图和终态收据强类型契约；`L2Observation` 显式携带 `frame_id/frame_sequence`。
- `backend/app/services/data_trunk.py:40-240`：删除旧 `transact/evaluate_due_formulas/advance_freshness` 外部语义，将 `DataTrunk` 收口为黑板与帧处理 facade。
- `backend/app/services/data_trunk_conversion.py:21-430`：为每帧生成稳定且不重复的 L2 event；`STALE` 保留计算值；支持按拓扑逐输出计算。
- `backend/app/services/data_trunk_postgres.py:1-1810`：把旧单事务 adapter 硬切为 `PostgresFrameRepository`，复用类型列编码、批量写和来源证据 SQL。
- `backend/app/services/data_trunk_outbox.py:1-230`：从逐 L2 JSON 事件改为逐终态帧领取，并在领取后按 `frame_id` 查询 committed L0/L2 形成不可变事件包。
- `backend/app/services/pipeline.py:35-590`：MQTT 解析后直接写黑板，删除缓冲、flush 重试账本和逐批告警旁路。
- `backend/app/main.py:160-315`：启动一个捕获循环、一个帧处理循环和一个帧 outbox 循环；停止启动旧 freshness、typed-formula 及 F1/F2/F3 旁路调度器。
- `backend/app/services/point_processing.py:257-282,555-580` 与 `backend/app/services/point_processing_postgres.py:1191-1530`：点位加工发布接入运行栅栏，旧帧/outbox 未排空时零写拒绝，成功后黑板切到新修订 WARMING。
- `backend/app/api/point_processings.py:28-42`：把 main 注册的运行栅栏注入点位加工 service，不新增第二个全局运行实例。
- `backend/app/services/configuration_revision.py`：定义稳定错误码 `CONFIGURATION_RUNTIME_BUSY`，不新增通用工作流。
- `backend/tests/test_pipeline_data_trunk.py`、`backend/tests/test_data_trunk_startup_gate.py`、`backend/tests/postgres_delivery_app.py`：替换旧 buffer/flush 测试缝。
- `backend/tests/test_data_trunk_conversion.py`、`backend/tests/test_point_processing_dag.py`：补 frame identity、STALE 保值和全量 DAG 回归。
- `scripts/test_build_release_images.py`：Schema 期望值从 045 更新为 046；平台版本不在本计划内猜测或升级。
- `CODEX_HANDOFF.md`：记录完成范围、验证证据和下一阶段边界。

### 保留但本阶段不接入公开页面

- `backend/app/api/websocket.py` 与 `frontend/` 不在本计划修改范围；第一阶段分支不得单独发布到 1 号机。
- 现有 `data_trunk_outbox.py` 的租约与指数退避实现思路保留，但外部事件单位改为整帧。

## 规格覆盖与有意分期

| 实时黑板规格验收项 | 本计划落点 |
|---|---|
| 1–5：WARMING、无额外变化零写、last-wins、STALE、全量 L1 | Tasks 1、4、6、8 |
| 6–8、12、14：A/B 隔离、恢复、失败预算、STALE 重建、DAG 下游闭包 | Tasks 3–5、8 |
| 9：REST 快照、游标与 WebSocket 原子增量 | 第二阶段；本计划只产出可订阅的 committed frame seam |
| 10：批量 SQL、无忙循环、30 秒既有延迟门禁 | Tasks 3、6、8 |
| 11：自动控制只读 committed GOOD L2 | 第三阶段；第一阶段不接控制消费者 |
| 13：配置发布排空、旧修订隔离与新修订 WARMING | Task 7 在真实 frame dispatcher 之后覆盖决定 L1 的点位加工发布；其余上层配置发布在第三阶段接入同一 seam |

容量治理不伪装成已完成：Task 8 输出 `rows/frame` 与 7 天容量估算；第四阶段发布门禁必须在这个证据上
确认并实现 L2 history、来源关系、frame metadata 的同步有界保留，以及 published outbox 的游标窗口。
未发布 outbox 永不按时间静默删除。

因此第一阶段只证明“数据帧生产与恢复底座”自身完整，不声称整份运行专项的页面和上层消费验收已经
完成，也不得单独形成生产发布候选。

---

### Task 1: 建立强类型帧契约与纯内存实时黑板

**Files:**
- Create: `backend/app/services/realtime_blackboard.py`
- Modify: `backend/app/services/data_trunk_contracts.py:38-352`
- Test: `backend/tests/test_realtime_blackboard.py`

**Interfaces:**
- Produces: `SourceOrderMode`、`SourceOrder.is_after()` 与 `RawObservation.source_order`
- Produces: `FramedRawObservation`, `FrozenFrameCandidate`, `BlackboardState`, `AcceptReceipt`
- Produces: `RealtimeBlackboard.accept_many()`, `tick()`, `acknowledge()`,
  `reset_revision(revision, active_input_contracts, required_tag_ids)`
- Invariant: `tick()` 每次都推进 `capture_beat`；仅 READY 且值或质量变化时返回冻结候选。
- Invariant: `required_tag_ids` 必须是 `active_input_contracts` 的子集；只有 required 阻塞 READY，已到达的
  optional/诊断 L0 同样属于黑板、帧和 latest。

- [ ] **Step 1: 写出重复、同拍 last-wins、READY、三拍 STALE 和冻结隔离的失败测试**

```python
class RealtimeBlackboardTest(unittest.TestCase):
    def test_warms_until_required_inputs_arrive_and_keeps_last_sample_per_beat(self):
        board = RealtimeBlackboard(active_input_contracts={
            TAG_A: SourceOrderMode.SEQUENCE,
            TAG_B: SourceOrderMode.SEQUENCE,
        }, required_tag_ids=frozenset({TAG_A, TAG_B}))
        self.assertTrue(board.accept_many((_raw(TAG_A, 1, 10.0),)).accepted_count)
        self.assertIsNone(board.tick(NOW, configuration_revision=7))
        board.accept_many((_raw(TAG_A, 2, 20.0), _raw(TAG_A, 1, 99.0)))
        board.accept_many((_raw(TAG_B, 1, 30.0),))
        frozen = board.tick(NOW + timedelta(seconds=1), configuration_revision=7)
        self.assertIsNotNone(frozen)
        self.assertEqual(20.0, frozen.cells[TAG_A].observation.value.value)
        self.assertEqual((TAG_A, TAG_B), tuple(item.observation.tag_id for item in frozen.changed_l0))

    def test_third_missed_beat_emits_one_stale_frame_without_erasing_value(self):
        board = _ready_board()
        first = board.tick(NOW, configuration_revision=3)
        board.acknowledge(first.generation)
        self.assertIsNone(board.tick(NOW + timedelta(seconds=1), configuration_revision=3))
        self.assertIsNone(board.tick(NOW + timedelta(seconds=2), configuration_revision=3))
        stale = board.tick(NOW + timedelta(seconds=3), configuration_revision=3)
        self.assertEqual(TrunkQuality.STALE, stale.cells[TAG_A].effective_quality)
        self.assertEqual(10.0, stale.cells[TAG_A].observation.value.value)
        self.assertEqual((), stale.changed_l0)

    def test_updates_after_freeze_are_reserved_for_the_next_frame(self):
        board = _ready_board()
        frozen = board.tick(NOW, configuration_revision=2)
        board.accept_many((_raw(TAG_A, 2, 40.0),))
        self.assertEqual(10.0, frozen.cells[TAG_A].observation.value.value)
        board.acknowledge(frozen.generation)
        next_frame = board.tick(NOW + timedelta(seconds=1), configuration_revision=2)
        self.assertEqual(40.0, next_frame.cells[TAG_A].observation.value.value)

    def test_optional_active_diagnostic_does_not_block_ready_and_enters_later_frame(self):
        board = RealtimeBlackboard(
            active_input_contracts={
                TAG_A: SourceOrderMode.SEQUENCE,
                TAG_DIAG: SourceOrderMode.SEQUENCE,
            },
            required_tag_ids=frozenset({TAG_A}),
        )
        board.accept_many((_raw(TAG_A, 1, 10.0),))
        first = board.tick(NOW, configuration_revision=7)
        self.assertIsNotNone(first)
        self.assertNotIn(TAG_DIAG, first.cells)
        board.acknowledge(first.generation)
        board.accept_many((_raw(TAG_DIAG, 1, 99.0),))
        second = board.tick(NOW + timedelta(seconds=1), configuration_revision=7)
        self.assertEqual(99.0, second.cells[TAG_DIAG].observation.value.value)
        self.assertIn(TAG_DIAG, {item.observation.tag_id for item in second.changed_l0})

    def test_revision_reset_reclassifies_draining_candidates_and_rewarms_changed_inputs(self):
        board = _ready_board(
            active_tags=(TAG_A, TAG_B, TAG_D, TAG_DIAG),
            required_tags=(TAG_A, TAG_B),
        )
        board.accept_many((
            _raw(TAG_A, 2, 11.0), _raw(TAG_B, 2, 22.0),
            _raw(TAG_D, 2, 44.0), _raw(TAG_DIAG, 2, 99.0),
        ))
        board.reset_revision(
            revision=8,
            active_input_contracts={
                TAG_A: SourceOrderMode.SEQUENCE,       # unchanged: keep newest candidate
                TAG_B: SourceOrderMode.RECEIVED_AT,    # changed: clear and await new sample
                TAG_C: SourceOrderMode.SEQUENCE,       # added: await new sample
                TAG_DIAG: SourceOrderMode.SEQUENCE,    # optional and unchanged: keep it
            },
            required_tag_ids=frozenset({TAG_A, TAG_B, TAG_C}),
        )
        self.assertEqual(BlackboardState.WARMING, board.state)
        self.assertEqual(frozenset({TAG_B, TAG_C}), board.missing_required_tags)
        board.accept_many((_raw_received(TAG_B, 23.0), _raw(TAG_C, 1, 33.0)))
        first_new = board.tick(NOW + timedelta(seconds=1), configuration_revision=8)
        self.assertEqual({TAG_A, TAG_B, TAG_C, TAG_DIAG}, set(first_new.cells))
        self.assertEqual(11.0, first_new.cells[TAG_A].observation.value.value)
        self.assertEqual(99.0, first_new.cells[TAG_DIAG].observation.value.value)
        self.assertNotIn(TAG_D, first_new.cells)
```

- [ ] **Step 2: 运行黑板测试并确认因模块不存在而失败**

Run: `cd backend && python -m unittest tests.test_realtime_blackboard -v`

Expected: FAIL with `ModuleNotFoundError: app.services.realtime_blackboard`.

- [ ] **Step 3: 增加不可变帧契约和黑板最小实现**

在 `data_trunk_contracts.py` 定义以下公开数据，不给调用方暴露内部 `_Cell`：

```python
class BlackboardState(str, Enum):
    WARMING = "WARMING"
    READY = "READY"

@dataclass(frozen=True)
class FramedRawObservation:
    observation: RawObservation
    accepted_beat: int
    effective_quality: TrunkQuality

@dataclass(frozen=True)
class FrozenFrameCandidate:
    frame_id: UUID
    candidate_digest: str
    generation: int
    capture_beat: int
    shot_at: datetime
    configuration_revision: int
    cells: Mapping[UUID, FramedRawObservation]
    changed_l0: tuple[FramedRawObservation, ...]

@dataclass(frozen=True)
class AcceptReceipt:
    accepted_count: int
    dropped_count: int
```

源顺序必须是同模式比较的结构化值，禁止把 `R/S/T` 前缀字符串互相比大小：

```python
class SourceOrderMode(str, Enum):
    SEQUENCE = "sequence"
    OBSERVED_AT = "observed_at"
    RECEIVED_AT = "received_at"

@dataclass(frozen=True)
class SourceOrder:
    mode: SourceOrderMode
    primary: int       # sequence 或规范 UTC epoch microseconds
    secondary: int     # receive epoch/ordinal；sequence 模式固定为 0
    tie_breaker: str

    def is_after(self, previous: "SourceOrder") -> bool:
        if self.mode is not previous.mode:
            raise DataTrunkError("DATA_FRAME_SOURCE_ORDER_MODE_MISMATCH")
        if self.mode is SourceOrderMode.SEQUENCE:
            return self.primary > previous.primary
        return (self.primary, self.secondary, self.tie_breaker) > (
            previous.primary, previous.secondary, previous.tie_breaker
        )
```

`TagMetadata` 固定一个 `source_order_mode`：只有配置明确声明源序号跨重连稳定时才允许 `SEQUENCE`；
当前 NanoMQ/Neuron 的 transport sequence 会重置，只保存作证据，默认不得承担运行排序。可信设备时间使用
`OBSERVED_AT`；否则使用 `RECEIVED_AT`。所有时间先转 UTC epoch microseconds；接收模式由一个从已提交
latest 恢复的单调 receive ordinal 打破同微秒平局。配置修订若改变 mode，必须清除该 tag 的旧比较基线并
重新 WARMING；同一修订内 mode 突变直接拒绝，不能跨模式猜先后。

`reset_revision()` 以数据库重载出的 `active_input_contracts[tag_id] = source_order_mode` 及其子集
`required_tag_ids` 为真相，并一次完成栅栏候选归类：新旧修订都 active 且 mode 未变的 tag（包括 optional
诊断点）保留切换期间收到的最后候选与比较基线，并把它归入新修订；不再 active 的 tag 连同候选/基线
删除；新加 active tag 或 mode 改变的 tag 清空候选和比较基线，必须等新修订激活后再来一条样本。所有旧
emitted signature 和冻结候选都清空，首张新修订帧重新发送全部**已有值的 active L0**；状态进入 WARMING，
但只等待 `required_tag_ids`，optional 未到不阻塞 READY，后到时进入后续帧/latest/outbox。不得把旧 mode 的
source order 或已移除 tag 带进新帧，也不得笼统丢弃同 tag、同 mode 的栅栏期新值。

`RealtimeBlackboard` 内部使用 `RLock`，`accept_many()` 仅在新 key 严格大于旧 key 时覆盖；新样本的
`accepted_beat` 固定为 `current_capture_beat + 1`。`tick()` 先加拍号，再计算漏拍；第 3 拍只发生一次
GOOD/UNCERTAIN/BAD → STALE 质量转换；冻结时用 `MappingProxyType` 和不可变 dataclass 复制全量视图。
冻结时生成一次稳定 `frame_id`，并对 frame identity、修订、拍号、shot time、按 tag 排序的全量 cell 身份及
changed L0 身份计算规范 SHA-256 `candidate_digest`。冻结候选在 `acknowledge(generation)` 前保持原对象可重试，后到更新写入下一 generation。候选等待事务 A 时，
每次站级 tick 仍递增拍号并推进下一 generation 的新鲜度，但只能重试原冻结候选；候选确认后，累积的
新值或质量变化在下一次 tick 冻结，不能覆盖或污染原候选。

- [ ] **Step 4: 运行测试并验证全部通过**

Run: `cd backend && python -m unittest tests.test_realtime_blackboard -v`

Expected: PASS；至少覆盖重复、迟到、last-wins、WARMING、READY、STALE、冻结隔离、UTC 归一化、
不同 mode 拒绝、相等 sequence 拒绝、receive ordinal 恢复及候选 identity/digest 重试稳定。

- [ ] **Step 5: 提交 Task 1**

```bash
git add backend/app/services/data_trunk_contracts.py backend/app/services/realtime_blackboard.py backend/tests/test_realtime_blackboard.py
git commit -m "feat: add realtime blackboard contracts"
```

---

### Task 2: Schema 046 帧结构与硬切约束

**Files:**
- Create: `init-db/migration_046_data_frames.sql`
- Create: `backend/tests/test_data_frames_migration_postgres.py`
- Create: `backend/tests/run_postgres_group.py`
- Modify: `backend/tests/test_data_trunk_migration_postgres.py`
- Modify: `scripts/test_build_release_images.py`

**Interfaces:**
- Produces: `t_data_frames`, `t_data_frame_outbox`
- Extends: `t_telemetry(frame_id, frame_sequence, accepted_beat, source_order_mode, source_receive_ordinal)`、`t_l2_observations(frame_id)`
- Extends: `t_telemetry_latest(frame_sequence)`、`t_l2_latest(frame_sequence)`
- Extends: `t_ingestion_failures(frame_id, stage='frame')` 作为 FAILED 的持久系统失败事实
- Invariant: 旧历史帧列允许 NULL；新帧运行写由 repository 强制非空。

- [ ] **Step 1: 写 Schema 046 新装、升级、重放、压缩块和损坏拒绝测试**

```python
def test_046_installs_single_site_frame_contract(self):
    self._apply_046()
    with self._connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.t_data_frames'), to_regclass('public.t_data_frame_outbox')")
        self.assertEqual(("t_data_frames", "t_data_frame_outbox"), cursor.fetchone())
        cursor.execute("SELECT to_regclass('public.t_l2_stream_outbox')")
        self.assertEqual((None,), cursor.fetchone())
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='t_data_frames'")
        columns = {row[0] for row in cursor.fetchall()}
        self.assertNotIn("site_id", columns)
        self.assertTrue({
            "frame_id", "frame_sequence", "candidate_digest", "capture_beat",
            "configuration_revision", "status", "processing_owner", "processing_token"
        } <= columns)

def test_046_refuses_unpublished_legacy_outbox_without_writes(self):
    self._insert_unpublished_legacy_outbox()
    with self.assertRaisesRegex(psycopg2.Error, "SCHEMA_046_OUTBOX_NOT_DRAINED"):
        self._apply_046()
    self.assertIsNone(self._regclass("public.t_data_frames"))

def test_046_replay_rejects_mutated_frame_constraint(self):
    self._apply_046()
    self._execute("ALTER TABLE t_data_frames DROP CONSTRAINT chk_data_frame_status")
    with self.assertRaisesRegex(psycopg2.Error, "SCHEMA_046_PARTIAL_STRUCTURE"):
        self._apply_046()

def test_046_adds_nullable_frame_columns_to_existing_compressed_l0_chunk(self):
    self._insert_old_l0_and_compress_its_chunk()
    self._apply_046()
    self.assertEqual((None, None, None), self._old_l0_frame_columns())
    self.assertTrue(self._old_l0_row_still_exists())

def test_046_marks_legacy_latest_with_sequence_zero(self):
    self._insert_legacy_l0_and_l2_latest()
    self._apply_046()
    self.assertEqual((0, 0), self._legacy_latest_sequences())

def test_046_allows_stale_l2_with_last_value_but_bad_must_be_empty(self):
    self._apply_046()
    self._insert_l2(quality=1, value_float=12.5, reason="SOURCE_STALE")
    with self.assertRaises(psycopg2.errors.CheckViolation):
        self._insert_l2(quality=0, value_float=12.5, reason="SOURCE_BAD")

def test_046_only_allows_empty_stale_for_failed_frame_without_baseline(self):
    self._apply_046()
    self._insert_l2(
        quality=1, value_float=None,
        reason="FRAME_PROCESSING_FAILED_NO_BASELINE",
    )
    with self.assertRaises(psycopg2.errors.CheckViolation):
        self._insert_l2(quality=1, value_float=None, reason="SOURCE_STALE")

def test_046_rejects_illegal_frame_transition(self):
    frame = self._insert_pending_frame()
    with self.assertRaises(psycopg2.errors.RaiseException):
        self._set_frame_status(frame, "COMPLETE")

def test_046_terminal_frame_is_immutable_and_undeletable(self):
    frame = self._insert_complete_frame_with_outbox()
    with self.assertRaises(psycopg2.errors.RaiseException):
        self._set_frame_status(frame, "FAILED")
    with self.assertRaises(psycopg2.errors.RaiseException):
        self._delete_frame(frame)

def test_046_failed_frame_requires_matching_failure_fact(self):
    frame = self._insert_processing_frame()
    self._insert_terminal_outbox(frame, status="FAILED")
    with self.assertRaises(psycopg2.errors.RaiseException):
        self._set_frame_status(frame, "FAILED", failure_code="FRAME_PROCESSING_FAILED")
```

同一测试文件还必须覆盖：fresh 046、045→046、第二次 replay、拒绝后零部分 DDL、真实压缩 chunk 上升级且
045 的 compression/retention job 不变、frame history 索引均为 `WHERE frame_id/frame_sequence IS NOT NULL`、
旧 `commit_sequence` 自动 default 已删除、两个 typed CHECK、验证函数和两个触发器的 footprint 被篡改时
replay fail closed。另测 framed L0 缺 accepted beat/order 字段被拒绝、legacy 五字段全空仍可读、两个 latest
的 sequence default 已删除、outbox claim 三字段半空被拒绝、terminal status 不匹配及终态 frame 缺 outbox
均在事务提交前失败。还要覆盖 `PENDING -> COMPLETE/FAILED` 等越级迁移被拒绝、终态不能更新或删除、
FAILED 缺 `failure_code`、缺唯一 frame failure fact、fact 的 code/digest 不匹配均在提交前失败；合法
`PENDING -> PROCESSING -> COMPLETE/FAILED`、受原 claim fencing 保护且清空 claim/error 的
`PROCESSING -> PENDING` 重试，以及租约过期后的 fencing takeover 仍可通过。同 owner/token 延长租约必须
被拒绝。

- [ ] **Step 2: 运行 PostgreSQL 迁移测试并确认失败**

Run:

```powershell
cd backend
$env:ZIZU_POSTGRES_TEST='1'
python tests/run_postgres_group.py tests.test_data_frames_migration_postgres
```

`run_postgres_group.py` 先检查 `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD` 全部存在、
`ZIZU_POSTGRES_TEST=1` 且 `DB_NAME.endswith('_test')`，再运行传入的 unittest modules；任何 skipped test、
failure 或 error 都返回非零。这样每条 PG 命令独立执行时也不会静默误绿。Expected: FAIL because migration 不存在。

- [ ] **Step 3: 编写 replay-safe Schema 046**

核心 DDL 固定为以下形状：

```sql
CREATE TABLE public.t_data_frames (
  frame_id UUID PRIMARY KEY,
  frame_sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
  candidate_digest CHAR(64) NOT NULL
    CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
  capture_beat BIGINT NOT NULL UNIQUE CHECK (capture_beat >= 1),
  shot_at TIMESTAMPTZ NOT NULL,
  configuration_revision BIGINT NOT NULL
    REFERENCES public.t_configuration_revisions(revision),
  status TEXT NOT NULL,
  attempt_count SMALLINT NOT NULL DEFAULT 0 CHECK (attempt_count BETWEEN 0 AND 3),
  processing_owner UUID,
  processing_token UUID,
  lease_until TIMESTAMPTZ,
  failure_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  CONSTRAINT chk_data_frame_status
    CHECK (status IN ('PENDING','PROCESSING','COMPLETE','FAILED')),
  CONSTRAINT uq_data_frame_identity_sequence UNIQUE (frame_id, frame_sequence),
  CONSTRAINT uq_data_frame_terminal_identity
    UNIQUE (frame_id, frame_sequence, status),
  CONSTRAINT chk_data_frame_terminal
    CHECK ((status IN ('COMPLETE','FAILED')) = (finished_at IS NOT NULL)),
  CONSTRAINT chk_data_frame_failure_code CHECK (
    (status = 'FAILED') = (failure_code IS NOT NULL)
  ),
  CONSTRAINT chk_data_frame_processing_lease CHECK (
    (status = 'PROCESSING'
      AND processing_owner IS NOT NULL AND processing_token IS NOT NULL
      AND lease_until IS NOT NULL)
    OR
    (status <> 'PROCESSING'
      AND processing_owner IS NULL AND processing_token IS NULL
      AND lease_until IS NULL)
  )
);

CREATE INDEX ix_data_frames_claim
  ON public.t_data_frames(status, lease_until, frame_sequence)
  WHERE status IN ('PENDING','PROCESSING');

ALTER TABLE public.t_telemetry
  ADD COLUMN frame_id UUID,
  ADD COLUMN frame_sequence BIGINT CHECK (frame_sequence >= 1),
  ADD COLUMN accepted_beat BIGINT CHECK (accepted_beat >= 1),
  ADD COLUMN source_order_mode TEXT
    CHECK (source_order_mode IN ('sequence','observed_at','received_at')),
  ADD COLUMN source_receive_ordinal BIGINT CHECK (source_receive_ordinal >= 0);
ALTER TABLE public.t_l2_observations
  ADD COLUMN frame_id UUID;
ALTER TABLE public.t_telemetry
  ADD CONSTRAINT fk_telemetry_data_frame FOREIGN KEY (frame_id, frame_sequence)
    REFERENCES public.t_data_frames(frame_id, frame_sequence) NOT VALID;
ALTER TABLE public.t_telemetry
  ADD CONSTRAINT chk_telemetry_frame_fields CHECK (
    (frame_id IS NULL AND frame_sequence IS NULL AND accepted_beat IS NULL
      AND source_order_mode IS NULL AND source_receive_ordinal IS NULL)
    OR
    (frame_id IS NOT NULL AND frame_sequence IS NOT NULL AND accepted_beat IS NOT NULL
      AND source_order_mode IS NOT NULL
      AND (source_order_mode <> 'received_at' OR source_receive_ordinal IS NOT NULL))
  ) NOT VALID;
ALTER TABLE public.t_l2_observations
  ADD CONSTRAINT fk_l2_observation_data_frame FOREIGN KEY (frame_id, commit_sequence)
    REFERENCES public.t_data_frames(frame_id, frame_sequence) NOT VALID;
ALTER TABLE public.t_telemetry_latest
  ADD COLUMN frame_sequence BIGINT NOT NULL DEFAULT 0 CHECK (frame_sequence >= 0),
  ADD COLUMN source_order_mode TEXT
    CHECK (source_order_mode IN ('sequence','observed_at','received_at')),
  ADD COLUMN source_receive_ordinal BIGINT CHECK (source_receive_ordinal >= 0);
ALTER TABLE public.t_l2_latest
  ADD COLUMN frame_sequence BIGINT NOT NULL DEFAULT 0 CHECK (frame_sequence >= 0);
ALTER TABLE public.t_telemetry_latest
  ADD CONSTRAINT chk_telemetry_latest_frame_fields CHECK (
    (frame_sequence = 0 AND source_order_mode IS NULL AND source_receive_ordinal IS NULL)
    OR
    (frame_sequence > 0 AND source_order_mode IS NOT NULL
      AND (source_order_mode <> 'received_at' OR source_receive_ordinal IS NOT NULL))
  );
ALTER TABLE public.t_telemetry_latest ALTER COLUMN frame_sequence DROP DEFAULT;
ALTER TABLE public.t_l2_latest ALTER COLUMN frame_sequence DROP DEFAULT;
ALTER TABLE public.t_l2_observations
  ALTER COLUMN commit_sequence DROP DEFAULT;
ALTER TABLE public.t_tags
  ADD COLUMN source_sequence_trusted BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX ix_telemetry_frame_tag
  ON public.t_telemetry(frame_id, tag_id) WHERE frame_id IS NOT NULL;
CREATE INDEX ix_telemetry_tag_frame_sequence
  ON public.t_telemetry(tag_id, frame_sequence DESC, ts DESC)
  WHERE frame_sequence IS NOT NULL;
CREATE INDEX ix_l2_observations_frame
  ON public.t_l2_observations(frame_id, entity_instance_id)
  WHERE frame_id IS NOT NULL;

ALTER TABLE public.t_ingestion_failures ADD COLUMN frame_id UUID
  REFERENCES public.t_data_frames(frame_id);
-- replay-safe implementation replaces the existing stage CHECK to include 'frame'
CREATE UNIQUE INDEX uq_ingestion_failure_frame
  ON public.t_ingestion_failures(frame_id) WHERE frame_id IS NOT NULL;

CREATE TABLE public.t_data_frame_outbox (
  frame_id UUID PRIMARY KEY,
  frame_sequence BIGINT NOT NULL UNIQUE,
  terminal_status TEXT NOT NULL CHECK (terminal_status IN ('COMPLETE','FAILED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_by UUID,
  claim_token UUID,
  claimed_until TIMESTAMPTZ,
  CHECK (
    (claimed_by IS NULL AND claim_token IS NULL AND claimed_until IS NULL)
    OR
    (claimed_by IS NOT NULL AND claim_token IS NOT NULL AND claimed_until IS NOT NULL)
  ),
  FOREIGN KEY (frame_id, frame_sequence, terminal_status)
    REFERENCES public.t_data_frames(frame_id, frame_sequence, status)
    DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX ix_data_frame_outbox_pending
  ON public.t_data_frame_outbox(frame_sequence)
  WHERE published_at IS NULL;
```

迁移同时创建 `guard_data_frame_transition()` 的 `BEFORE UPDATE OR DELETE` trigger。它把
`frame_id/frame_sequence/candidate_digest/capture_beat/shot_at/configuration_revision/created_at` 视为永久身份；
只允许：`PENDING -> PROCESSING` 领取；持有完整旧 owner+token 的 `PROCESSING -> PENDING` 可重试退回；
**旧租约已经过期**时 `PROCESSING -> PROCESSING` 换新 owner/token 接管；以及持有有效完整 claim 的
`PROCESSING -> COMPLETE/FAILED`。退回 PENDING 必须清空 owner/token/lease/failure_code，attempt 不回退；中间
错误只进结构化运行日志，不占用终态专属 `failure_code`。PROCESSING 同状态接管必须换 token，旧 lease 在
数据库事务时间已过期；预算未耗尽时 attempt 恰加 1，预算已耗尽的 terminalization claim 保持 attempt 不变。
首版禁止同 owner/token 续租或延长 `lease_until`，30 秒就是单次处理硬上限。任何终态 UPDATE、任何 frame
DELETE、`PENDING -> terminal`、terminal 回退或 COMPLETE/FAILED 互改都抛稳定错误码。repository 还必须用
owner+token 条件更新；trigger 是第二道状态图防线，不能代替 fencing WHERE。

同一迁移原子替换 `chk_l2_typed_value`、`chk_l2_latest_typed_value` 与
`validate_l2_typed_value_against_entity()`：`BAD` 必须零 typed value；`UNCERTAIN/GOOD` 必须一个类型匹配值；
`STALE` 通常必须保留一个类型匹配值，只有没有任何上一终态的 FAILED 首帧允许零值，且 reason 必须严格等于
`FRAME_PROCESSING_FAILED_NO_BASELINE`。不得用宽松的 `quality=1 AND value_count IN (0,1)` 放过其他空 STALE。

再增加三个 deferred evidence gate：frame 从非终态转为 COMPLETE/FAILED 时，事务提交前必须恰好存在一条
同 status outbox；FAILED 还必须恰好存在一条 `stage='frame'` 的 failure fact，并满足
`source_digest = candidate_digest`、`safe_summary->>'code' = failure_code`；空值 STALE 的 L2 history/latest 在
提交前必须证明所属 frame 为 FAILED，且同实体在该帧前确实没有任何 typed terminal baseline，否则拒绝。
这样 repository 写入顺序不受限，但仅伪造 reason 字符串不能绕过约束；published outbox 到期后的清理由后续
保留策略执行，不触发 frame 状态更新。

FAILED 的 failure fact 通过唯一 `frame_id` 关联，不在 outbox 重复保存 failure payload 或外键。
`t_data_frames` 的 `(frame_id,capture_beat,candidate_digest,configuration_revision)` 是事务 A 结果未知时的
幂等身份；新帧 sequence 从 1 开始，latest 中 `frame_sequence=0` 明确表示迁移前值，不给旧 history 伪造帧。

迁移开头先确认完整 045，并在任何 DDL 前检查旧 `t_l2_stream_outbox` 没有 `published_at IS NULL`；有未发布
行就抛 `SCHEMA_046_OUTBOX_NOT_DRAINED`。完整旧 footprint 才执行创建和删除旧表；完整新 footprint
重放为 no-op；混合或缺约束/索引统一抛 `SCHEMA_046_PARTIAL_STRUCTURE`。不得给旧历史伪造 `frame_id`。
[TimescaleDB 官方说明](https://docs.timescale.com/use-timescale/latest/compression/modify-a-schema/)显示 2.6+
支持向已压缩 hypertable 添加 nullable column，但实现仍须用测试中的真实压缩 chunk 证明 2.29.2 环境
可升级且不重写旧事实；[约束文档](https://docs.timescale.com/use-timescale/latest/schema-management/about-constraints/)
说明 hypertable 可引用普通表。迁移必须先加无默认 nullable 列，再加 `NOT VALID` FK 以立即约束新写入、避免
扫描旧压缩历史；测试要证明非法新 frame 被拒绝、045 policy 保留并记录 DDL 锁时长，不得只在空库验证。

- [ ] **Step 4: 更新迁移链和 release schema 契约后运行定向测试**

把 `scripts/test_build_release_images.py` 中 schema 断言改为 `046`；在既有 data-trunk migration reset/apply
helper 中追加 `_apply_045()`、`_apply_046()`，不改 001–045 文件。

Run:

```powershell
cd backend
$env:ZIZU_POSTGRES_TEST='1'
python tests/run_postgres_group.py tests.test_data_trunk_migration_postgres tests.test_data_frames_migration_postgres tests.test_edge_storage_retention_migration_postgres
cd ..
python -m unittest scripts.test_build_release_images -v
```

Expected: PASS；PostgreSQL 组 0 skip，release builder 断言最新 Schema 为 046。

- [ ] **Step 5: 提交 Task 2**

```bash
git add init-db/migration_046_data_frames.sql backend/tests/run_postgres_group.py backend/tests/test_data_frames_migration_postgres.py backend/tests/test_data_trunk_migration_postgres.py scripts/test_build_release_images.py
git commit -m "feat: add data frame schema"
```

---

### Task 3: 实现单写者、运行恢复与事务 A

**Files:**
- Modify: `backend/app/services/data_trunk.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/app/services/data_trunk_contracts.py`
- Create: `backend/tests/test_data_frames_postgres.py`

**Interfaces:**
- Produces: `PostgresFrameRepository.acquire_writer() -> FrameWriterLease`
- Produces: `restore_blackboard() -> BlackboardRecovery`
- Produces: `commit_pending(candidate: FrozenFrameCandidate) -> PendingFrame`
- Consumes: Task 1 `FrozenFrameCandidate`
- Invariant: 事务 A 失败或返回结果未知时冻结候选仍以同一 identity 重试；获得确定 `PendingFrame` 后调用方才 `acknowledge()`。

- [ ] **Step 1: 写事务 A 不公开 latest/L2/outbox和唯一 writer 的失败测试**

```python
def test_transaction_a_persists_pending_frame_and_changed_l0_only(self):
    candidate = self._candidate(configuration_revision=1, capture_beat=4)
    pending = self.repository.commit_pending(candidate)
    self.assertEqual("PENDING", pending.status.value)
    with self.connection.cursor() as cursor:
        cursor.execute("SELECT status, capture_beat FROM t_data_frames WHERE frame_id=%s", (pending.frame_id,))
        self.assertEqual(("PENDING", 4), cursor.fetchone())
        cursor.execute("SELECT count(*) FROM t_telemetry WHERE frame_id=%s", (pending.frame_id,))
        self.assertEqual((len(candidate.changed_l0),), cursor.fetchone())
        cursor.execute("SELECT count(*) FROM t_telemetry_latest WHERE frame_sequence=%s", (pending.frame_sequence,))
        self.assertEqual((0,), cursor.fetchone())
        cursor.execute("SELECT count(*) FROM t_l2_observations WHERE frame_id=%s", (pending.frame_id,))
        self.assertEqual((0,), cursor.fetchone())
        cursor.execute("SELECT count(*) FROM t_data_frame_outbox WHERE frame_id=%s", (pending.frame_id,))
        self.assertEqual((0,), cursor.fetchone())

def test_second_writer_is_rejected_while_first_lease_is_held(self):
    first = self.repository.acquire_writer()
    self.addCleanup(first.close)
    with self.assertRaisesRegex(DataTrunkError, "DATA_FRAME_WRITER_ALREADY_ACTIVE"):
        self.other_repository.acquire_writer()

def test_transaction_a_commit_response_loss_returns_original_frame(self):
    candidate = self._candidate(configuration_revision=1, capture_beat=5)
    self.repository.raise_after_commit_once = True
    with self.assertRaisesRegex(DataTrunkError, "DATA_FRAME_COMMIT_RESULT_UNKNOWN"):
        self.repository.commit_pending(candidate)
    recovered = self.repository.commit_pending(candidate)
    self.assertEqual(candidate.frame_id, recovered.frame_id)
    self.assertEqual(1, self._frame_count(capture_beat=5))
    self.assertEqual(len(candidate.changed_l0), self._l0_count(recovered.frame_id))

def test_transaction_a_rejects_same_capture_identity_with_different_digest(self):
    original = self._candidate(capture_beat=6)
    self.repository.commit_pending(original)
    with self.assertRaisesRegex(DataTrunkError, "DATA_FRAME_CANDIDATE_CONFLICT"):
        self.repository.commit_pending(_mutate_candidate_digest(original))
```

- [ ] **Step 2: 运行定向测试并确认旧 repository 缺少接口**

Run: `cd backend; $env:ZIZU_POSTGRES_TEST='1'; python tests/run_postgres_group.py tests.test_data_frames_postgres`

Expected: FAIL with missing `commit_pending` or `acquire_writer`.

- [ ] **Step 3: 把 PostgreSQL adapter 的第一个深接口写完整**

`data_trunk_contracts.py` 增加：

```python
class FrameStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"

@dataclass(frozen=True)
class PendingFrame:
    frame_id: UUID
    frame_sequence: int
    capture_beat: int
    shot_at: datetime
    configuration_revision: int
    status: FrameStatus

@dataclass(frozen=True)
class BlackboardRecovery:
    capture_beat: int
    configuration_revision: int
    active_input_contracts: Mapping[UUID, SourceOrderMode]
    required_tag_ids: frozenset[UUID]
    observations: tuple[FramedRawObservation, ...]
```

writer lease 只负责一条专用数据库连接和一次解锁，重复 `close()` 必须安全：

```python
class FrameWriterLease:
    def __init__(self, connection, release_connection: Callable[[Any], None]) -> None:
        self._connection = connection
        self._release_connection = release_connection
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        with self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(hashtextextended('zizu:data-frame-writer', 0))"
            )
        self._release_connection(self._connection)
        self._closed = True
```

`acquire_writer()` 用一条独占 session advisory lock：

```sql
SELECT pg_try_advisory_lock(hashtextextended('zizu:data-frame-writer', 0));
```

`FrameWriterLease.close()` 在同一专用连接执行 `pg_advisory_unlock` 后归还连接；不得用事务锁冒充进程生命期
租约。`restore_blackboard()` 从最大 `capture_beat`、当前配置修订、全部 active L1 输入及其 source-order
mode、其中 required tag 子集，以及所有已保存 L0 的
结构化 source order 与 receive ordinal 重建基线；恢复出来的值只作 order/value 基线，`seen_this_revision=False`，所以启动
仍为 WARMING。

`commit_pending()` 在一个事务中：取得 `zizu:data-frame-capture` xact advisory lock、复核
`t_configuration_state.current_revision`，按 `frame_id` 与唯一 `capture_beat` 查找既有帧：四项 identity
完全一致就返回原 `frame_id/frame_sequence`；任一 digest/revision/beat 不一致就抛
`DATA_FRAME_CANDIDATE_CONFLICT`。不存在时插入调用方提供的 `frame_id/candidate_digest`，再用一次
`execute_values` 写 `candidate.changed_l0`，并写 `frame_id/frame_sequence/accepted_beat`。revision 不同返回
`DATA_FRAME_CONFIGURATION_STALE`，不写任何行。故障注入必须专门放在数据库 commit 成功后、Python 返回前，
证明结果未知重试不重复 frame 或 L0。

- [ ] **Step 4: 运行事务 A、旧迁移和批量写回归**

Run:

```powershell
cd backend
$env:ZIZU_POSTGRES_TEST='1'
python tests/run_postgres_group.py tests.test_data_frames_postgres tests.test_data_trunk_bulk_write tests.test_data_trunk_startup_gate
```

Expected: PASS；1000 个变化 L0 仍由固定数量批量 SQL 完成，不出现逐点 commit。

- [ ] **Step 5: 提交 Task 3**

```bash
git add backend/app/services/data_trunk.py backend/app/services/data_trunk_contracts.py backend/app/services/data_trunk_postgres.py backend/tests/test_data_frames_postgres.py
git commit -m "feat: persist pending data frames"
```

---

### Task 4: 按固定修订运行完整 L1 并完成事务 B

**Files:**
- Create: `backend/app/services/frame_processor.py`
- Modify: `backend/app/services/data_trunk_conversion.py`
- Modify: `backend/app/services/data_trunk_contracts.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Create: `backend/tests/test_frame_processor.py`
- Modify: `backend/tests/test_data_frames_postgres.py`
- Modify: `backend/tests/test_data_trunk_conversion.py`

**Interfaces:**
- Produces: `FrameRepository.claim_next()`, `load_processing_snapshot()`, `complete()`
- Produces: `FrameProcessor.process_next(now: datetime) -> TerminalFrame | None`
- Consumes: Task 3 `PendingFrame`
- Invariant: 每帧每个活动即时 L1 输出只求值一次；frame identity 进入 L2 event identity。

- [ ] **Step 1: 写全量拓扑、STALE 保值、终态原子性和序号防倒退测试**

```python
def test_processor_evaluates_each_active_output_once_in_dag_order(self):
    repository = InMemoryFrameRepository(frame=_pending_frame(), snapshot=_cross_node_snapshot())
    processor = FrameProcessor(repository, evaluator=evaluate_processing, clock=lambda: NOW)
    terminal = processor.process_next(NOW)
    self.assertEqual(FrameStatus.COMPLETE, terminal.status)
    self.assertEqual((PCS_POWER_ID, SITE_POWER_ID), repository.evaluated_entity_ids)
    self.assertEqual(terminal.frame_id, repository.outputs[0].frame_id)
    self.assertEqual(terminal.frame_sequence, repository.outputs[0].frame_sequence)

def test_stale_input_keeps_last_value_and_propagates_stale_quality(self):
    output = evaluate_processing(
        installed=(NUMERIC_PROCESSING,),
        current_inputs={InputReference.l0(TAG_A): _raw(TAG_A, quality=TrunkQuality.STALE, value=1000.0)},
        configuration_revision=4,
        calculated_at=NOW,
        frame_id=FRAME_ID,
        frame_sequence=9,
    )[0]
    self.assertEqual(1.0, output.value.value)
    self.assertEqual(TrunkQuality.STALE, output.quality)

def test_pure_stale_complete_advances_l0_latest_without_new_history(self):
    before = self._l0_history_count()
    terminal = self._complete_pure_stale_frame()
    latest = self._l0_latest(TAG_A)
    self.assertEqual(TrunkQuality.STALE, latest.quality)
    self.assertEqual(terminal.frame_sequence, latest.frame_sequence)
    self.assertEqual(before, self._l0_history_count())

def test_transaction_b_publishes_everything_or_nothing(self):
    self.repository.fault_hook = lambda stage: (_ for _ in ()).throw(RuntimeError()) if stage == "source" else None
    with self.assertRaises(DataTrunkError):
        self.processor.process_next(NOW)
    self.assertEqual("PROCESSING", self._frame_status())
    self.assertEqual(0, self._latest_rows_for_frame())
    self.assertEqual(0, self._outbox_rows_for_frame())

def test_first_committed_frame_advances_legacy_sequence_zero_latest(self):
    self._insert_legacy_latest_before_046()
    terminal = self._migrate_and_complete_first_frame()
    self.assertEqual(
        (terminal.frame_sequence, terminal.frame_sequence),
        self._l0_and_l2_latest_sequences(),
    )
```

- [ ] **Step 2: 运行 processor、conversion 和 PostgreSQL 测试并确认失败**

Run pure tests first, then the guarded PG module:

```powershell
cd backend
python -m unittest tests.test_frame_processor tests.test_data_trunk_conversion -v
$env:ZIZU_POSTGRES_TEST='1'
python tests/run_postgres_group.py tests.test_data_frames_postgres
```

Expected: FAIL because `FrameProcessor` and frame-aware evaluator arguments do not exist.

- [ ] **Step 3: 实现固定修订的完整 DAG 求值**

把帧处理接口的数据形状固定下来，后续任务不得改名：

```python
@dataclass(frozen=True)
class ClaimedFrame:
    frame_id: UUID
    frame_sequence: int
    capture_beat: int
    shot_at: datetime
    configuration_revision: int
    attempt_count: int
    processing_owner: UUID
    processing_token: UUID
    lease_until: datetime
    created_at: datetime

@dataclass(frozen=True)
class ProcessingSnapshot:
    l0_by_tag: Mapping[UUID, FramedRawObservation]
    installed_by_entity_id: Mapping[UUID, InstalledPointProcessing]
    topological_output_ids: tuple[UUID, ...]
    dependency_edges: tuple[tuple[UUID, UUID], ...]

    def current_inputs(self) -> dict[InputReference, RawObservation | L2Observation]:
        return {
            InputReference.l0(tag_id): replace(
                cell.observation,
                quality=cell.effective_quality,
            )
            for tag_id, cell in self.l0_by_tag.items()
        }

@dataclass(frozen=True)
class TerminalFrame:
    frame_id: UUID
    frame_sequence: int
    configuration_revision: int
    status: FrameStatus
    finished_at: datetime
```

把 `L2Observation` 增加必填 `frame_id` 和 `frame_sequence`，并把 `frame_id` 放进 `_observation()` 的
UUIDv5 event material，确保两个不同帧即使输入值未变也产生不同 event identity。

`FrameProcessor` 只暴露一个运行方法：

```python
class FrameProcessor:
    def process_next(self, now: datetime) -> TerminalFrame | None:
        claimed = self._repository.claim_next(now)
        if claimed is None:
            return None
        snapshot = self._repository.load_processing_snapshot(claimed)
        current_inputs = snapshot.current_inputs()
        outputs: list[L2Observation] = []
        for entity_id in snapshot.topological_output_ids:
            installed = snapshot.installed_by_entity_id[entity_id]
            produced = self._evaluator(
                installed=(installed,),
                current_inputs=current_inputs,
                configuration_revision=claimed.configuration_revision,
                calculated_at=now,
                frame_id=claimed.frame_id,
                frame_sequence=claimed.frame_sequence,
            )[0]
            outputs.append(produced)
            current_inputs[InputReference.l2(entity_id)] = produced
        return self._repository.complete(claimed, snapshot, tuple(outputs))
```

`load_processing_snapshot()` 必须加载帧固定的 configuration revision、该 revision 的已安装 L1、冻结的
全量 L0 及跨节点依赖边。用 `validate_processing_dag()` 的稳定 order；没有边的活动输出按 UUID 排序并
加入一次。不得使用旧 `_evaluate_batch()` 的“只算受本批 L0 影响的输出”。

`complete()` 在事务 B 内一次完成：

1. 先以 `frame_id + processing_owner + processing_token + attempt_count + 未过期 lease` 锁定唯一 claim；不匹配就抛 `DATA_FRAME_CLAIM_LOST`，整个事务零写；
2. 从完整 `ProcessingSnapshot` 批量推进全部活动 `t_telemetry_latest`，包括没有新 history 的纯 STALE 质量
   转换；以 `EXCLUDED.frame_sequence > current.frame_sequence` 比较，迁移前 latest 已是哨兵 0；
3. 批量写全部活动 L2 history，`commit_sequence=frame_sequence` 且 `frame_id` 非空；
4. L2 latest 只比较 `frame_sequence`；
5. 写实际 L0/L2 来源关系；
6. 插入一条含同一 `frame_sequence` 的 `t_data_frame_outbox`；
7. 把帧置 `COMPLETE`、清 owner/token/lease、写 `finished_at`；
8. 单次 commit 后才返回 `TerminalFrame`。

删除 `_select_history_observations()` 的逐实体心跳语义。修改 numeric/enum/fault-code/boolean-set/formula：
STALE 输入仍用最后值计算并输出 STALE；只有 BAD 或类型/单位错误清空值。

- [ ] **Step 4: 运行正常帧主缝和既有 DAG 回归**

Run:

```powershell
cd backend
python -m unittest tests.test_frame_processor tests.test_data_trunk_conversion tests.test_point_processing_dag -v
$env:ZIZU_POSTGRES_TEST='1'
python tests/run_postgres_group.py tests.test_data_frames_postgres
```

Expected: PASS；L0/L2 的 `frame_id/configuration_revision/frame_sequence` 一致，旧帧重放不能倒退 latest。

- [ ] **Step 5: 提交 Task 4**

```bash
git add backend/app/services/frame_processor.py backend/app/services/data_trunk_contracts.py backend/app/services/data_trunk_conversion.py backend/app/services/data_trunk_postgres.py backend/tests/test_frame_processor.py backend/tests/test_data_frames_postgres.py backend/tests/test_data_trunk_conversion.py
git commit -m "feat: complete committed data frames"
```

---

### Task 5: PENDING 恢复、租约、有限重试与 FAILED 闭环

**Files:**
- Modify: `backend/app/services/frame_processor.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/app/services/data_trunk_contracts.py`
- Modify: `backend/tests/test_frame_processor.py`
- Modify: `backend/tests/test_data_frames_postgres.py`

**Interfaces:**
- Produces: `retry_or_fail(claimed, failure: FrameFailure, now: datetime) -> TerminalFrame | None`
- Produces: `BudgetTerminalizationClaim`、`fail_budget(claimed, now) -> TerminalFrame`
- Produces: `downstream_closure(failed_ids, edges) -> frozenset[UUID]`
- Invariant: 第 3 次或年龄 60 秒终结；失败也不删除事务 A 的 L0。

- [ ] **Step 1: 写租约重领、纯 STALE 重建、下游闭包和失败继续测试**

```python
def test_expired_processing_lease_is_reclaimed_without_duplicate_results(self):
    first = self.repository.claim_next(NOW)
    self.clock.advance(seconds=31)
    second = self.other_repository.claim_next(self.clock.now())
    self.assertEqual(first.frame_id, second.frame_id)
    self.assertEqual(2, second.attempt_count)
    self.assertNotEqual(first.processing_token, second.processing_token)
    with self.assertRaisesRegex(DataTrunkError, "DATA_FRAME_CLAIM_LOST"):
        self.repository.complete(first, self._outputs_for(first))
    self.assertEqual(0, self._terminal_rows(first.frame_id))

def test_recovery_rebuilds_stale_from_capture_and_accepted_beat(self):
    snapshot = self.repository.load_processing_snapshot(self._pure_stale_pending_frame())
    self.assertEqual(TrunkQuality.STALE, snapshot.l0_by_tag[TAG_A].quality)
    self.assertEqual(10.0, snapshot.l0_by_tag[TAG_A].value.value)

def test_third_failure_stales_transitive_downstream_and_continues(self):
    closure = downstream_closure(
        frozenset({PCS_POWER_ID}),
        ((PCS_POWER_ID, ESS_POWER_ID), (ESS_POWER_ID, SITE_POWER_ID)),
    )
    self.assertEqual(frozenset({PCS_POWER_ID, ESS_POWER_ID, SITE_POWER_ID}), closure)
    terminal = self.repository.retry_or_fail(_claim(attempt_count=3), _failure(PCS_POWER_ID), NOW)
    self.assertEqual(FrameStatus.FAILED, terminal.status)
    self.assertEqual(3, self._stale_l2_count(terminal.frame_id))
    self.assertEqual(1, self._system_failure_count(terminal.frame_id))
    self.assertEqual(next_frame_id, self.repository.claim_next(NOW).frame_id)

def test_first_failed_frame_uses_explicit_no_baseline_stale_without_fake_value(self):
    terminal = self.repository.retry_or_fail(
        _claim(attempt_count=3), _failure(PCS_POWER_ID), NOW
    )
    observation = self._failed_l2(terminal.frame_id, PCS_POWER_ID)
    self.assertEqual(TrunkQuality.STALE, observation.quality)
    self.assertIsNone(observation.value.value)
    self.assertEqual("FRAME_PROCESSING_FAILED_NO_BASELINE", observation.reason)

def test_third_claim_crash_is_terminalized_after_lease_expiry(self):
    third = self._claim(attempt_count=3)
    self._simulate_worker_crash(third)
    self.clock.advance(seconds=31)
    terminalization = self.other_repository.claim_next(self.clock.now())
    self.assertIsInstance(terminalization, BudgetTerminalizationClaim)
    self.assertEqual(3, terminalization.attempt_count)
    terminal = self.other_repository.fail_budget(terminalization, self.clock.now())
    self.assertEqual(FrameStatus.FAILED, terminal.status)

def test_budget_terminalization_from_old_pending_stales_all_active_l2_and_unblocks_next_frame(self):
    old = self._insert_pending(created_at=NOW - timedelta(seconds=60), attempt_count=1)
    next_frame = self._insert_pending(created_at=NOW, attempt_count=0)
    claim = self.repository.claim_next(NOW)
    self.assertIsInstance(claim, BudgetTerminalizationClaim)
    self.assertEqual(FrameStatus.PROCESSING, self._frame_row(old.frame_id).status)
    terminal = self.repository.fail_budget(claim, NOW)
    self.assertEqual(set(self._all_active_l2_ids(claim.configuration_revision)),
                     set(self._stale_l2_ids(terminal.frame_id)))
    self.assertEqual(next_frame.frame_id, self.repository.claim_next(NOW).frame_id)

def test_sixty_second_old_pending_frame_fails_without_l1_evaluation(self):
    self._insert_pending(created_at=NOW - timedelta(seconds=60), attempt_count=1)
    self.processor.process_next(NOW)
    self.assertEqual(0, self.evaluator.call_count)
    self.assertEqual(1, self._system_failure_count())

def test_retry_returns_to_pending_and_clears_terminal_only_error_fields(self):
    first = self.repository.claim_next(NOW)
    self.repository.retry_or_fail(first, _failure(PCS_POWER_ID), NOW)
    row = self._frame_row(first.frame_id)
    self.assertEqual("PENDING", row.status)
    self.assertIsNone(row.processing_owner)
    self.assertIsNone(row.processing_token)
    self.assertIsNone(row.lease_until)
    self.assertIsNone(row.failure_code)

def test_active_processing_lease_cannot_be_renewed(self):
    claimed = self.repository.claim_next(NOW)
    with self.assertRaisesRegex(DataTrunkError, "DATA_FRAME_LEASE_RENEWAL_FORBIDDEN"):
        self._attempt_same_token_lease_extension(claimed, seconds=10)
```

- [ ] **Step 2: 运行定向测试并确认失败行为尚不存在**

Run:

```powershell
cd backend
python -m unittest tests.test_frame_processor -v
$env:ZIZU_POSTGRES_TEST='1'
python tests/run_postgres_group.py tests.test_data_frames_postgres
```

Expected: FAIL on reclaim/failure methods.

- [ ] **Step 3: 实现有界恢复和 fail-closed 终结**

```python
@dataclass(frozen=True)
class FrameFailure:
    code: str
    failed_entity_ids: frozenset[UUID]

def downstream_closure(
    failed_ids: frozenset[UUID],
    edges: tuple[tuple[UUID, UUID], ...],
) -> frozenset[UUID]:
    outgoing: dict[UUID, set[UUID]] = {}
    for source, target in edges:
        outgoing.setdefault(source, set()).add(target)
    affected = set(failed_ids)
    pending = list(failed_ids)
    while pending:
        current = pending.pop()
        for target in outgoing.get(current, set()):
            if target not in affected:
                affected.add(target)
                pending.append(target)
    return frozenset(affected)
```

`claim_next(now)` 先取单站 processor xact advisory lock，只检查最小非终态 `frame_sequence`：头帧仍有有效
租约就返回空，绝不能跳到后帧。领取前先检查 `attempt_count >= 3` 或 `now-created_at >= 60s`；命中预算时
原子设置 `status='PROCESSING'` 并写入新的 owner/token/30 秒 terminalization lease，但**不递增
attempt_count、不运行 L1**；`BudgetTerminalizationClaim.affected_l2` 固定为冻结配置修订下的全部活动 L2，
然后立即走同一个 FAILED 事务。这样无法定位具体求值点时仍全量 fail-closed。terminalizer 自己崩溃后仍可
用新 token 再领取，计数永远不超过 3。只有预算未耗尽的 PENDING/过期 PROCESSING 才原子增加
`attempt_count`，写入 runtime owner、新 UUID token 和
固定 30 秒租约。`complete()`、`retry_or_fail()` 及 FAILED 终结都必须在任何写入前用完整 claim identity 与
`lease_until > now` fencing；影响行数不是 1 就抛 `DATA_FRAME_CLAIM_LOST`。首版不续租：30 秒就是单帧处理
硬上限，过期 worker 必须丢弃本地结果。

`load_processing_snapshot()` 正常进程可使用冻结视图；恢复路径执行一条 set-based 查询，从
`t_telemetry` 取每个活动 tag 在 `frame_sequence <= claimed.frame_sequence` 的最后真实观测，
并按 `claimed.capture_beat - accepted_beat >= 3` 重算 STALE。恢复不得使用当前时钟猜质量。

处理异常时，`FrameProcessor` 记录正在求值的 entity ID；能定位就对 dependency edges 求传递下游闭包，
不能定位就选择全部活动 L2。若 `attempt_count < 3` 且 `now-created_at < 60s`，用完整 owner+token fencing 把
状态放回 PENDING，并清空 claim、lease、finished_at、failure_code；中间失败 code 只写结构化运行日志，下一次
重新领取。否则一个事务完成：推进该帧 L0 latest、读取受影响 L2 上一终态值、写保值 STALE L2
（有基线 reason=`FRAME_PROCESSING_FAILED`；无基线仅写空 STALE 且 reason=
`FRAME_PROCESSING_FAILED_NO_BASELINE`）、写来源、向 `t_ingestion_failures` 幂等插入一条 `stage='frame'`
系统失败事实、插入同 `frame_id` 的 FAILED outbox、帧置 FAILED。failure safe summary 只含 code、revision、
frame sequence 与受影响实体 ID，不含原始值或凭据；上述内容和终态必须同事务提交或回滚。第三阶段再把这条
durable fact 适配为统一告警中心事件，第一阶段不得谎称 no-op publisher 已经“告警”。

正常 `complete()` 必须接收 `ProcessingSnapshot` 与 outputs；事务 B 用 snapshot 的**全部活动 L0**推进
`t_telemetry_latest` 的 observation/value/effective quality/frame sequence。纯 STALE 帧复用上一真实
observation，quality 改为 STALE、sequence 改为当前帧，但绝不向 `t_telemetry` 伪造 history 行。恢复出来的
同一纯 STALE snapshot 必须产生完全相同 latest。

- [ ] **Step 4: 运行恢复、事务故障和防倒退回归**

Run:

```powershell
cd backend
python -m unittest tests.test_frame_processor -v
$env:ZIZU_POSTGRES_TEST='1'
python tests/run_postgres_group.py tests.test_data_frames_postgres tests.test_data_trunk_bulk_write
```

Expected: PASS；事务 A 的 L0 在三次失败后仍存在；FAILED 只有一个持久失败事实和一个终态 outbox；
旧 token 晚到零写；后一帧可继续完成。

- [ ] **Step 5: 提交 Task 5**

```bash
git add backend/app/services/frame_processor.py backend/app/services/data_trunk_contracts.py backend/app/services/data_trunk_postgres.py backend/tests/test_frame_processor.py backend/tests/test_data_frames_postgres.py
git commit -m "feat: recover and fail data frames safely"
```

---

### Task 6: 把 MQTT 管道和生命周期切到唯一站级节拍

**Files:**
- Modify: `backend/app/services/data_trunk.py`
- Modify: `backend/app/services/pipeline.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_pipeline_data_trunk.py`
- Modify: `backend/tests/test_data_trunk_startup_gate.py`
- Modify: `backend/tests/postgres_delivery_app.py`
- Create: `backend/tests/test_data_frame_runtime.py`

**Interfaces:**
- Produces: `DataTrunk.accept()`, `capture_tick()`, `process_next()`
- Consumes: Tasks 1–5 blackboard/repository/processor
- Invariant: MQTT path不写数据库；只有 capture loop 创建事务 A；只有 processor loop执行事务 B。

- [ ] **Step 1: 写单一节拍、60 秒无变化零写和旧旁路停止启动的失败测试**

```python
class DataFrameRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_only_accepts_canonical_l0_into_blackboard(self):
        trunk = RecordingFrameTrunk()
        pipeline = DataPipeline(data_trunk=trunk)
        await pipeline.on_message(_mqtt_message(sequence=9, value=12345))
        self.assertEqual((9,), tuple(item.source_sequence for item in trunk.accepted))
        self.assertEqual(0, trunk.capture_calls)

    async def test_sixty_empty_ticks_after_stale_transition_create_no_more_frames(self):
        runtime = _ready_runtime()
        runtime.capture_tick(NOW)
        runtime.process_next(NOW)
        for second in range(1, 4):
            runtime.capture_tick(NOW + timedelta(seconds=second))
            runtime.process_next(NOW + timedelta(seconds=second))
        writes_after_stale = runtime.repository.pending_write_count
        for second in range(4, 64):
            runtime.capture_tick(NOW + timedelta(seconds=second))
        self.assertEqual(writes_after_stale, runtime.repository.pending_write_count)

    async def test_slow_transaction_a_does_not_block_mqtt_accept_or_shutdown(self):
        repository = BlockingPendingRepository()
        runtime = _ready_runtime(repository)
        capture = asyncio.create_task(asyncio.to_thread(runtime.capture_tick, NOW))
        await repository.pending_started.wait()
        receipt = runtime.accept((_raw(sequence=2),))
        self.assertEqual(1, receipt.accepted_count)
        runtime.request_stop()
        repository.release_pending.set()
        await asyncio.wait_for(capture, timeout=1)

def test_main_starts_only_frame_runtime_loops(self):
    source = inspect.getsource(main.lifespan)
    self.assertIn("data_frame_capture", source)
    self.assertIn("data_frame_processor", source)
    self.assertNotIn("data_trunk_freshness", source)
    self.assertNotIn("data_trunk_typed_formulas", source)
    self.assertNotIn("run_formula_tick", source)
    self.assertNotIn("run_rule_tick", source)
    self.assertNotIn("run_aggregation_tick", source)
```

- [ ] **Step 2: 运行 runtime/pipeline/startup 测试并确认旧 buffer 语义失败**

Run: `cd backend && python -m unittest tests.test_data_frame_runtime tests.test_pipeline_data_trunk tests.test_data_trunk_startup_gate -v`

Expected: FAIL because pipeline still calls `ingest()` and main starts legacy loops.

- [ ] **Step 3: 硬切 `DataTrunk` 与 `DataPipeline` 外部接口**

`DataTrunk` 公开面固定为：

```python
class DataTrunk:
    def accept(self, observations: Sequence[RawObservation]) -> AcceptReceipt:
        return self._blackboard.accept_many(tuple(observations))

    def capture_tick(self, now: datetime) -> PendingFrame | None:
        candidate = self._blackboard.tick(now, self._repository.current_configuration_revision())
        if candidate is None:
            return None
        pending = self._repository.commit_pending(candidate)
        self._blackboard.acknowledge(candidate.generation)
        return pending

    def process_next(self, now: datetime) -> TerminalFrame | None:
        return self._processor.process_next(now)
```

`DataPipeline.on_message()` 完成 parse + canonical adapter 后直接调用 `self._data_trunk.accept(raw_observations)`；
删除 `_buffer`、`_flush_loop`、`MAX_INGEST_ATTEMPTS`、旧 terminal ingest failure ledger 以及 raw/L2 告警旁路。
测试模拟器把 `flush_now()` 改为显式调用 `capture_tick()` 与 `process_next()`，不保留内部兼容别名。

MQTT 热路径只允许短持有黑板内存锁。所有同步 PostgreSQL、DAG 和 outbox repository 调用必须离开 asyncio
事件循环：`main` 统一使用 `await asyncio.to_thread(runtime.capture_tick, now)`、
`await asyncio.to_thread(runtime.process_next, now)`；后续 Task 7 的 claim/mark 同样如此。不得在黑板锁内等待
数据库、网络发布或配置事务。增加慢 repository 测试，证明 capture/processor 卡住时 MQTT accept、健康路由和
优雅停止仍能推进。

`main.lifespan` 获取单 writer lease、恢复黑板后再启动 MQTT；创建：

- `data_frame_capture`：用 monotonic deadline 每 1 秒调用一次 `capture_tick()`，不累积忙循环；
- `data_frame_processor`：处理到无 PENDING 后等待事件或 250ms；

Task 7 再增加 `data_frame_outbox` 循环；Task 6 不启动尚不存在的 publisher。

停止启动旧 5 秒 freshness、1 秒 typed formula，以及 `aggregator/formula_engine/rule_engine` 三条旁路调度器。
这些文件本任务不删除，第三阶段统一做引用对账后再决定物理删除。

- [ ] **Step 4: 运行唯一节拍、非阻塞和旧旁路回归**

配置发布栅栏依赖真实 outbox dispatcher，留到 Task 7 在同一任务内接入；本任务不得直接 SQL 标 published
或放一个 no-op consumer 伪造排空。

Run:

```powershell
cd backend
python -m unittest tests.test_data_frame_runtime tests.test_pipeline_data_trunk tests.test_data_trunk_startup_gate -v
$env:ZIZU_POSTGRES_TEST='1'
python tests/run_postgres_group.py tests.test_data_frames_postgres
```

Expected: PASS；MQTT accept 不做 DB I/O，慢 capture/processor 不阻塞事件循环，且 production main 只启动
frame capture/processor 两条主干循环。

- [ ] **Step 5: 提交 Task 6**

```bash
git add backend/app/services/data_trunk.py backend/app/services/pipeline.py backend/app/main.py backend/tests/test_data_frame_runtime.py backend/tests/test_pipeline_data_trunk.py backend/tests/test_data_trunk_startup_gate.py backend/tests/postgres_delivery_app.py
git commit -m "feat: run data trunk on frame cadence"
```

---

### Task 7: 统一终态帧 outbox

**Files:**
- Modify: `backend/app/services/data_trunk_outbox.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/services/data_trunk.py`
- Modify: `backend/app/services/point_processing.py`
- Modify: `backend/app/services/point_processing_postgres.py`
- Modify: `backend/app/services/configuration_revision.py`
- Modify: `backend/app/api/point_processings.py`
- Create: `backend/tests/test_data_frame_outbox.py`
- Modify: `backend/tests/test_data_frames_postgres.py`
- Modify: `backend/tests/test_entity_observation_websocket.py`
- Modify: `backend/tests/test_data_trunk_startup_gate.py`
- Modify: `backend/tests/test_data_frame_runtime.py`
- Modify: `backend/tests/test_point_processing_postgres.py`

**Interfaces:**
- Produces: `CommittedL0Change`, `CommittedL2Change`, `FrameOutboxEvent`, `PostgresFrameOutboxRepository`, `FrameOutboxDispatcher`
- Produces: `ConfigurationRuntimeGate.begin_configuration_publish()`
- Produces: `reconcile_configuration_runtime() -> RuntimeRevision`
- Invariant: 一终态帧一条 outbox；outbox 行不存整帧 JSON；只有 publisher 成功后标 published。
- Invariant: 配置事务可能已提交后，数据库当前修订是唯一真相；内存未成功对齐时必须保持 QUIESCED。
- Scope seam: 本任务发布到进程内 `CommittedFramePublisher`；公开 REST/WebSocket cursor 协议由第二阶段接入。

- [ ] **Step 1: 写纯 STALE 重建、头阻塞顺序、失败退避和无消费者门禁测试**

```python
class DataFrameOutboxTest(unittest.IsolatedAsyncioTestCase):
    async def test_dispatches_one_atomic_event_per_terminal_frame(self):
        event = FrameOutboxEvent(
            frame_id=FRAME_ID,
            frame_sequence=12,
            status=FrameStatus.COMPLETE,
            configuration_revision=4,
            l0_changes=(_committed_l0(L0_A), _committed_l0(L0_B)),
            l2_changes=(_committed_l2(L2_A), _committed_l2(L2_B)),
            failure_id=None,
        )
        repository = InMemoryFrameOutboxRepository((event,))
        publisher = RecordingCommittedFramePublisher()
        dispatched = await FrameOutboxDispatcher(repository, publisher).run_once()
        self.assertEqual(1, dispatched)
        self.assertEqual((event,), tuple(publisher.events))
        self.assertEqual((FRAME_ID,), repository.published_ids)

    async def test_publish_failure_releases_claim_with_backoff(self):
        repository = InMemoryFrameOutboxRepository((_event(),))
        dispatcher = FrameOutboxDispatcher(repository, FailingPublisher())
        self.assertEqual(0, await dispatcher.run_once())
        self.assertEqual(1, repository.attempts[FRAME_ID])
        self.assertGreater(repository.next_attempt_at[FRAME_ID], NOW)

    async def test_failed_head_blocks_later_frame_until_retry_succeeds(self):
        repository = InMemoryFrameOutboxRepository((_event(sequence=10), _event(sequence=11)))
        publisher = FailOncePublisher(frame_sequence=10)
        dispatcher = FrameOutboxDispatcher(repository, publisher)
        self.assertEqual(0, await dispatcher.run_once())
        self.assertIsNone(await dispatcher.run_once(now=NOW + timedelta(seconds=1)))
        self.assertEqual((), publisher.successful_sequences)
        self.assertEqual(1, await dispatcher.run_once(now=NOW + timedelta(seconds=2)))
        self.assertEqual((10,), publisher.successful_sequences)

def test_pure_stale_frame_reconstructs_full_l0_state_at_target_beat(self):
    event = self.repository.load_event(self._terminal_pure_stale_frame())
    state = next(item for item in event.l0_changes if item.tag_id == TAG_A)
    self.assertEqual(LAST_REAL_OBSERVATION_ID, state.observation_id)
    self.assertEqual(10.0, state.value.value)
    self.assertEqual(TrunkQuality.STALE, state.effective_quality)

def test_release_mode_rejects_missing_real_committed_frame_consumer(self):
    with self.assertRaisesRegex(DataTrunkError, "COMMITTED_FRAME_CONSUMER_MISSING"):
        build_release_runtime(committed_frame_publisher=None)

def test_configuration_publish_refuses_unpublished_frame_without_consumer(self):
    self._complete_one_frame_without_consumer()
    with self.assertRaisesRegex(DataTrunkError, "COMMITTED_FRAME_CONSUMER_MISSING"):
        self.point_processing.apply(self._plan())
    self.assertEqual(0, self._configuration_write_count())

async def test_configuration_publish_drains_outbox_through_real_dispatcher(self):
    self._complete_one_frame()
    publisher = RecordingCommittedFramePublisher()
    apply_task = asyncio.create_task(asyncio.to_thread(self.point_processing.apply, self._plan()))
    await self.gate.entered_draining.wait()
    self.assertEqual(1, await FrameOutboxDispatcher(self.outbox, publisher).run_once())
    application = await asyncio.wait_for(apply_task, timeout=1)
    self.assertEqual(application.configuration_revision, self.runtime.configuration_revision)
    self.assertEqual(BlackboardState.WARMING, self.runtime.blackboard_state)

async def test_configuration_commit_then_reset_failure_stays_quiesced_and_reconciles_database_revision(self):
    self.runtime.fail_next_reset = True
    with self.assertRaisesRegex(DataTrunkError, "CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED"):
        await asyncio.to_thread(self.point_processing.apply, self._plan())
    committed = self.repository.load_current_runtime_revision()
    self.assertEqual(GateState.QUIESCED, self.gate.state)
    self.assertNotEqual(committed.revision, self.runtime.configuration_revision)
    reconciled = self.point_processing.reconcile_configuration_runtime()
    self.assertEqual(committed.revision, reconciled.revision)
    self.assertEqual(BlackboardState.WARMING, self.runtime.blackboard_state)

async def test_configuration_commit_result_unknown_reloads_database_before_running(self):
    self.repository.raise_after_commit_once = True
    with self.assertRaisesRegex(DataTrunkError, "CONFIGURATION_COMMIT_RESULT_UNKNOWN"):
        await asyncio.to_thread(self.point_processing.apply, self._plan())
    self.assertEqual(GateState.QUIESCED, self.gate.state)
    current = self.repository.load_current_runtime_revision()
    self.point_processing.reconcile_configuration_runtime()
    self.assertEqual(current.revision, self.runtime.configuration_revision)

async def test_optional_diagnostic_survives_revision_switch_and_reaches_latest_and_outbox(self):
    await self._apply_revision_that_keeps_optional_diag_same_mode()
    terminal = await self._capture_and_complete_first_new_revision_frame()
    self.assertEqual(99.0, self._l0_latest(TAG_DIAG).value.value)
    event = self.outbox.load_event(terminal.frame_id)
    self.assertIn(TAG_DIAG, {change.tag_id for change in event.l0_changes})
```

- [ ] **Step 2: 运行 outbox 测试并确认旧 `OutboxEvent` 形状失败**

Run:

```powershell
cd backend
python -m unittest tests.test_data_frame_outbox -v
$env:ZIZU_POSTGRES_TEST='1'
python tests/run_postgres_group.py tests.test_data_frames_postgres
```

Expected: FAIL because current outbox claims `t_l2_stream_outbox` JSON rows.

- [ ] **Step 3: 把 outbox 深模块硬切到 frame identity**

```python
@dataclass(frozen=True)
class CommittedL0Change:
    tag_id: UUID
    observation_id: UUID
    value: TypedValue
    source_quality: TrunkQuality
    effective_quality: TrunkQuality
    source_timestamp: datetime
    received_at: datetime
    accepted_beat: int

@dataclass(frozen=True)
class CommittedL2Change:
    entity_instance_id: UUID
    event_id: UUID
    value: TypedValue
    quality: TrunkQuality
    reason: str | None

@dataclass(frozen=True)
class FrameOutboxEvent:
    frame_id: UUID
    frame_sequence: int
    status: FrameStatus
    configuration_revision: int
    l0_changes: tuple[CommittedL0Change, ...]
    l2_changes: tuple[CommittedL2Change, ...]
    failure_id: UUID | None

class CommittedFramePublisher(Protocol):
    async def publish(self, event: FrameOutboxEvent) -> None:
        raise NotImplementedError
```

`claim_unpublished()` 先取得单站 outbox xact advisory lock，只查看最小未发布 `frame_sequence`；若该头帧
仍在退避或有效租约中就返回空，绝不能用 `SKIP LOCKED` 跳到后帧。领取写 owner + 一次性 claim token +
30 秒租约。`mark_published()` 与 `record_attempt()` 都校验完整 token；保留 2/4/8/16/32/60 秒上限退避。

领取后按目标 frame sequence 用 set-based 查询重建**原子终态增量包**，而不是只列“本帧插入的 ID”：

1. 对每个活动 tag 分别重建目标帧与前一终态帧的有效状态：取 `t_telemetry.frame_sequence <= target` 的最后
   真实观测，再用各自 `capture_beat - accepted_beat` 重算质量；只发送 observation identity、值或有效质量发生
   变化的 tag。第一张新主干帧发送全部可得活动 L0，因此纯 STALE、延迟分发和重启都能得到同一增量；
2. L2 增量只读取 `frame_id=target` 的已提交 rows：COMPLETE 本来就包含全部活动即时 L2，FAILED 只包含本帧
   被转为 STALE 的受影响项；消费者严格按帧序应用，不从当前 latest 猜历史；
3. FAILED event 必须按同一 `frame_id` 读取同事务产生的 `failure_id/failure_code`；
4. outbox 本身仍只保存 frame identity、顺序和投递元数据，不保存这些值。

第一阶段只建立 seam 和测试 adapter。没有真实 consumer 时，`main` **不启动 dispatcher、不标 published**，
release startup gate 固定返回 `COMMITTED_FRAME_CONSUMER_MISSING`；第二阶段把同一 seam 接到“完整 REST
快照 + 帧游标 + WebSocket 增量”后才解除这一项。旧
`EntityObservationBroadcaster/OutboxEvent` 只暂留给尚未迁移的公开 WebSocket 模块，使第一阶段代码可导入，
但 production main 不再向它发布，且它绝不能访问旧表或形成第二条运行数据路。对应逐实体
repository/dispatcher 测试删除，认证和订阅测试保留；第二阶段连同旧 endpoint 一次硬切删除。前端本阶段不改。

- [ ] **Step 4: 在真实 dispatcher 之后接入配置修订栅栏**

点位加工发布先检查已注册的 publisher 确实能确认送达；缺失时立即返回
`COMMITTED_FRAME_CONSUMER_MISSING` 且配置零写，不能进入 5 秒假等待。测试成功路径必须启动真实
`FrameOutboxDispatcher + RecordingCommittedFramePublisher` 排空，不准 SQL 手改 `published_at`。

有 consumer 时，`begin_configuration_publish(base_revision, timeout_seconds=5)` 使用同一个 runtime gate：

1. 在 gate 下从 RUNNING 进入 DRAINING，停止新 capture，但继续 MQTT `accept()`；processor/outbox 可继续
   领取并排空已经存在的旧修订帧；
2. `capture_tick()` 从冻结到事务 A + acknowledge 全程登记 inflight；processor 在进入 `claim_next()` **之前**
   必须在同一 gate 登记 claim intent，claim/complete/retry/fail 完成后才注销；
3. 队列初步为空后，发布线程仍持有 gate 把状态改为 CLOSING，阻止新的 claim intent，再等待所有已登记
   capture/processor 归零；等待通过 condition 释放内存 mutex，不阻塞在途 worker 注销；
4. 在 CLOSING 且仍禁止新 claim 的条件下复查无冻结候选、数据库 PENDING/PROCESSING=0、未发布 outbox=0；
   若又出现旧工作就回到 DRAINING，否则原子进入 QUIESCED；
5. QUIESCED 内完成 L1 配置与 configuration revision 的同一数据库事务。只有明确“提交前回滚”才允许用
   旧 runtime contract 恢复；数据库已提交、提交结果未知、或提交后 `reset_revision()` 失败时必须保持
   QUIESCED，capture 与 processor claim 继续关闭，绝不能猜测回旧修订；
6. `reconcile_configuration_runtime()` 用新连接重读数据库当前活动 revision、全部 active tag 的 source-order
   mode 与其中 required tag 子集，以数据库为唯一真相调用
   `reset_revision(revision, active_input_contracts, required_tag_ids)`；成功才回到
   RUNNING/WARMING。失败继续 QUIESCED 并返回 `CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED`。进程启动也必须
   在启动 capture/processor 前执行同一 reconciliation；commit-result-unknown 重试再按配置命令幂等键查询
   已提交 application，不重复创建修订。

DRAINING 时 MQTT `accept()` 继续收数据，但每个候选保留 tag 与当时的 source-order mode。第 6 步 reset
严格执行 Task 1 的栅栏归类：同 active tag 同 mode 的最后候选（含 optional 诊断点）进入新修订；不再
active 的 tag 丢弃；新增或 mode 改变的 tag 清基线并等待激活后的新样本，且只有 required 子集阻塞
WARMING。边界测试必须覆盖同一栅栏里“保留、删除、重新预热、optional 不阻塞但仍入帧”四种情况，证明
既不把旧输入契约带进新帧，也不无故丢弃可安全沿用的新值。

`complete/retry_or_fail` 只允许已登记的 in-flight claim 终结；发布方必须等它结束，不能在新 revision 激活后
让旧帧晚提交。增加边界并发测试：把 processor 阻塞在 claim 前，证明它要么先登记且发布等待，要么在
CLOSING/QUIESCED 被拒绝，绝不存在“零队列检查后又领取旧帧”。

`PointProcessingService.apply()` 在 gate 内使用原有 transaction；`api/point_processings.py` 的 async handler
必须 `await asyncio.to_thread(service.apply, command)`，保证最长 5 秒的排空不阻塞健康路由。这里只接决定
L1 的点位加工发布；告警/JDM/控制修订门禁在第三阶段复用同一 seam，不新建通用工作流。

- [ ] **Step 5: 运行 outbox、配置栅栏、租约和事务终态回归**

Run:

```powershell
cd backend
python -m unittest tests.test_data_frame_outbox tests.test_entity_observation_websocket tests.test_data_frame_runtime -v
$env:ZIZU_POSTGRES_TEST='1'
python tests/run_postgres_group.py tests.test_data_frames_postgres tests.test_point_processing_postgres
```

Expected: PASS；同一 `frame_id` 重试不产生第二条 outbox；PENDING/PROCESSING 不可领取；frame 10 失败时
frame 11 不越过；纯 STALE 在延迟/重启后仍重建相同终态；缺 consumer 配置稳定零写拒绝；测试 consumer
排空后才能切换修订，切换后重新 WARMING。

- [ ] **Step 6: 提交 Task 7**

```bash
git add backend/app/services/data_trunk_outbox.py backend/app/services/data_trunk_postgres.py backend/app/services/data_trunk.py backend/app/services/point_processing.py backend/app/services/point_processing_postgres.py backend/app/services/configuration_revision.py backend/app/api/point_processings.py backend/app/main.py backend/tests/test_data_frame_outbox.py backend/tests/test_data_frames_postgres.py backend/tests/test_entity_observation_websocket.py backend/tests/test_data_trunk_startup_gate.py backend/tests/test_data_frame_runtime.py backend/tests/test_point_processing_postgres.py
git commit -m "feat: publish frames and gate configuration"
```

---

### Task 8: 合同门禁、纵向机器验收与旧语义清除

**Files:**
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/app/services/data_trunk.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_data_trunk_startup_gate.py`
- Create: `backend/tests/test_data_frame_acceptance_postgres.py`
- Modify: `CODEX_HANDOFF.md`

**Interfaces:**
- Produces: Schema 046 startup contract gate
- Produces: 一条公开 canonical L0 → frame → L1 → committed L2 → frame outbox 验收证据
- Produces: 第一阶段 release-readiness blocker report（不是发布许可）
- Invariant: 发现旧 outbox、旧单事务入口或不完整 Schema 时运行启动 fail closed；缺消费者或未定有界保留时 release preflight fail closed。

- [ ] **Step 1: 写主缝和静态旧语义门禁测试**

```python
def test_public_frame_trunk_acceptance(self):
    runtime = self._runtime_with_published_brand_a_processing()
    runtime.accept(self._all_required_pcs_l0(sequence=1))
    pending = runtime.capture_tick(NOW)
    terminal = runtime.process_next(NOW + timedelta(milliseconds=20))
    self.assertEqual(FrameStatus.COMPLETE, terminal.status)
    evidence = self._frame_evidence(terminal.frame_id)
    self.assertEqual(1, evidence["frame_rows"])
    self.assertEqual(1, evidence["outbox_rows"])
    self.assertGreater(evidence["l0_rows"], 0)
    self.assertGreater(evidence["l2_rows"], 0)
    self.assertEqual({terminal.frame_sequence}, evidence["latest_sequences"])
    self.assertEqual({terminal.configuration_revision}, evidence["configuration_revisions"])

def test_failed_frame_acceptance_has_one_failure_fact_and_one_ordered_outbox(self):
    terminal = self._drive_frame_to_third_failure()
    evidence = self._frame_evidence(terminal.frame_id)
    self.assertEqual(1, evidence["system_failure_rows"])
    self.assertEqual(1, evidence["outbox_rows"])
    self.assertEqual(terminal.frame_sequence, evidence["outbox_frame_sequence"])

def test_first_stage_is_explicitly_not_release_ready(self):
    blockers = self._release_readiness_blockers()
    self.assertEqual(
        {"COMMITTED_FRAME_CONSUMER_MISSING", "DATA_FRAME_RETENTION_POLICY_UNRESOLVED"},
        blockers,
    )

def test_production_source_has_no_legacy_runtime_path(self):
    pipeline_source = inspect.getsource(DataPipeline)
    trunk_source = inspect.getsource(DataTrunk)
    postgres_source = inspect.getsource(PostgresFrameRepository)
    self.assertNotIn("_buffer", pipeline_source)
    self.assertNotIn("flush_now", pipeline_source)
    self.assertNotIn("evaluate_due_formulas", trunk_source)
    self.assertNotIn("mark_expired_outputs_stale", trunk_source)
    self.assertNotIn("t_l2_stream_outbox", postgres_source)
    self.assertNotIn("_select_history_observations", postgres_source)
```

- [ ] **Step 2: 运行验收并确认合同门禁尚未覆盖完整 046**

Run:

```powershell
cd backend
python -m unittest tests.test_data_trunk_startup_gate -v
$env:ZIZU_POSTGRES_TEST='1'
python tests/run_postgres_group.py tests.test_data_frame_acceptance_postgres
```

Expected: FAIL until startup gate checks all Schema 046 objects and runtime uses only frame path.

- [ ] **Step 3: 完成 startup contract gate 和机器证据**

`verify_data_trunk_contract_gate()` 增加数据库检查：

- `t_data_frames`、`t_data_frame_outbox` 存在，`t_l2_stream_outbox` 不存在；
- L0/L2 history 与 latest 的帧列完整；
- frame status、终态、唯一 sequence、claim/outbox 索引完整；
- candidate digest/capture beat 幂等约束、processing/outbox fencing token、legacy latest 序号 0、STALE typed
  CHECK/函数/触发器 fingerprint 完整；
- 当前只有一个活动 writer；启动恢复先处理 PENDING/过期 PROCESSING，再接受新的首帧；
- 当前配置修订能加载一套确定的 required L0 与完整 DAG。

验收测试必须证明：WARMING、首帧、完成 STALE 转换后连续 60 秒无额外写入、同拍 last-wins、三拍
STALE、事务 A 后崩溃恢复、
事务 A commit 后响应丢失幂等、事务 B 故障不公开半帧、旧租约晚到零写、第三次 FAILED + 单一系统失败
事实、后一帧继续、latest 不倒退、outbox 头阻塞有序、一帧一 outbox。测试报告直接用数据库
查询结果断言，不生成手写“通过”文本。

同一验收夹具连续写至少 100 张“1000 个活动 L0 每帧全部变化、100 个即时 L2”的最坏代表帧，避开单帧固定
开销误差，并分别记录：

- L0 行/帧、L2 行/帧、source 行/L2、outbox 行/帧；
- `t_telemetry`、`t_telemetry_latest`、`t_data_frames`、`t_l2_observations`、
  `t_l2_observation_sources`、`t_data_frame_outbox` 的 heap、全部 index、TOAST 与
  `pg_total_relation_size` 前后增量；`t_telemetry` 必须单列 Schema 046 新增五列和两个 partial recovery
  index 的实际增量，不能拿 045 的 7 天保留策略代替测量；
- `t_telemetry_latest` 虽是定长最新表，也要记录首帧扩表、持续 UPDATE 产生的 index/heap 膨胀以及 VACUUM 后
  稳态；
- 按最坏 `86,400 frames/day × 7 days` 外推 L0 history（含两个恢复 index）和全部即时证据的容量上界；
- consumer 中断 24 小时时未发布 outbox/backlog 的行数与字节，以及现场数据盘剩余空间覆盖时长。

原始测量和公式写入 handoff。这个证据只用于第四阶段确认同步保留窗口、published cursor horizon、未发布积压
告警/运维预算；本阶段不得据此擅自宣布容量可上线，未发布 outbox 仍不得靠超时删除。

- [ ] **Step 4: 运行所有相关门禁和完整后端套件**

Pure/runtime group:

```powershell
cd backend
python -m unittest tests.test_realtime_blackboard tests.test_frame_processor tests.test_data_frame_runtime tests.test_pipeline_data_trunk tests.test_data_trunk_conversion tests.test_point_processing_dag tests.test_data_frame_outbox tests.test_data_trunk_startup_gate -v
```

PostgreSQL group（只准隔离 `*_test` 数据库）：

```powershell
$env:ZIZU_POSTGRES_TEST='1'
python tests/run_postgres_group.py tests.test_data_frames_migration_postgres tests.test_data_frames_postgres tests.test_data_frame_acceptance_postgres tests.test_node_data_trunk_hard_cut_migration_postgres tests.test_edge_storage_retention_migration_postgres tests.test_point_processing_postgres
```

Complete backend and syntax gate:

```powershell
python -m unittest discover -s tests -v
python -m compileall app
cd ..
python -m unittest discover -s scripts -v
git diff --check
```

Expected: 所有非环境测试通过；显式 PostgreSQL 组 0 skip；完整 discovery 只允许仓库已记录、与本改动无关
且有证据的环境 skip，不允许 data-frame 测试 skip。若完整套件发现回归，修复后从对应失败命令完整重跑。

- [ ] **Step 5: 更新 handoff 并提交 Task 8**

`CODEX_HANDOFF.md` 写明提交范围、每组通过数、Schema 046、容量证据、release blockers、未实现的第二/三/四
阶段和“未构建候选、未部署 1 号机”。

```bash
git add backend/app/services/data_trunk.py backend/app/services/data_trunk_postgres.py backend/app/main.py backend/tests/test_data_trunk_startup_gate.py backend/tests/test_data_frame_acceptance_postgres.py CODEX_HANDOFF.md
git commit -m "test: prove committed frame data trunk"
```

---

## 完成定义

只有同时满足以下条件，第一阶段才可报告完成：

1. Schema 046 真实 PostgreSQL 新装、045 升级、重放、损坏拒绝全部通过；
2. 单 writer、WARMING/READY、last-wins、迟到丢弃、三拍 STALE 和保值语义均有黑盒测试；
3. 事务 A/B 的故障注入证明 L0 不丢、半帧不可见、latest 不倒退；
4. 完整即时 L1 DAG 每帧只运行一次，frame/configuration/source 证据一致；
5. PENDING/PROCESSING/outbox 重启恢复、claim fencing、3 次/60 秒 FAILED 与单一 durable failure 闭环通过；
6. 生产启动不再运行旧 freshness、typed formula、aggregator、formula engine 或 rule engine 旁路；
7. 数据库只有一张逐终态帧 outbox，纯 STALE 可重建、严格按 frame sequence 头阻塞发布，旧 `t_l2_stream_outbox` 和生产调用均为零；
8. 完整后端、scripts、compileall、diff check 均取得新鲜通过证据；
9. 没有修改前端、公开 WebSocket 协议或部署文件，也没有连接 1 号机；release preflight 恰好报告缺真实 consumer 与未确认有界保留两个 blocker。

完成后立即进入第二阶段“实时界面”独立规格/计划：REST 完整终态快照、帧游标、WebSocket 原子增量、
游标过旧重读和 L0/L2 页面收口。随后第三阶段把告警/JDM/控制/工作台全部收口到 committed L2，第四阶段
完成 EMS 参考纵向验收、同步有界保留、备份恢复演练和独立发布计划。第一、二、三阶段分支都不得单独
部署到生产；只有第四阶段另行确认后才可能部署 1 号机。
