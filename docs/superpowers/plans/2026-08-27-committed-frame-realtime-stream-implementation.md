# Committed Frame Realtime Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让节点详情通过一次 committed L0/L2 快照和一条有序数据帧流显示实时数据，并以固定补帧及保留策略解除数据帧底座的两个发布阻断。

**Architecture:** 新增深模块 `CommittedFrameStream`，把一致快照、scope 游标、durable replay、live buffer、过滤、排序和过期判断藏在一个 seam 后。Schema 047 把不可变 delta payload 写进每帧 outbox，Schema 048 增加 L2 小时/日汇总和受控清理；REST 与 WebSocket 只适配该模块，React 用纯投影状态机一次应用完整帧。

**Tech Stack:** Python 3.12、FastAPI、asyncio、psycopg2、PostgreSQL 16、TimescaleDB 2.29.2、React 18、TypeScript 5.5、Vite、Node test runner

**Spec:** `docs/superpowers/specs/2026-08-27-committed-frame-realtime-stream-design.md`

## Global Constraints

- 数据库是真相来源；事务 B 提交前不得发布或显示帧。
- 送达语义固定为 ordered at-least-once；客户端以 `frame_sequence` 幂等去重，不增加逐帧 ACK。
- 首版页面只订阅一个 `node_id`；底层 scope 可解析明确的 L0/L2 身份集合。
- 已发布 outbox 最长一小时且最多 5000 帧；未发布或已 claim 的行不得按时间清理。
- 普通 L0/L2/帧秒级历史保留七天；latest 与长期证据不得被级联破坏。
- 单客户端 live buffer 固定 64 帧；溢出时关闭连接并要求重新快照。
- 不增加 Redis、Kafka、新运行旁路、新依赖或现场可配置保留策略。
- 新链路验收后硬删除旧 `/ws/telemetry`、旧 `/ws/entity-observations` 和 L0 1.5 秒数据库轮询。
- 本计划不修改 EMS 工作台、告警、JDM、控制，不构建镜像，不部署 1 号机。

## File Structure

- `backend/app/services/committed_frame_stream.py`：公共领域类型、游标、深模块、内存 adapter 和 64 帧 hub。
- `backend/app/services/committed_frame_stream_postgres.py`：一致快照、scope 解析、durable replay 的 PostgreSQL adapter。
- `backend/app/api/committed_frames.py`：快照 REST、统一 WebSocket、身份与错误包络。
- `backend/app/services/data_trunk_outbox.py`：只保留统一帧 dispatcher 与 Schema 047 outbox adapter；删除旧逐实体实现。
- `init-db/migration_047_committed_frame_payload.sql`：版本化 immutable delta payload。
- `init-db/migration_048_frame_retention.sql`：L2 汇总、受控 L2/source/frame/outbox 清理与固定 job。
- `frontend/src/api/committedFrameStream.ts`：快照与统一 WebSocket 客户端。
- `frontend/src/components/data-trunk/committedFrameProjection.ts`：纯前端投影与帧去重状态机。
- `frontend/src/components/data-trunk/DataTrunkWorkspace.tsx`：节点 scope 生命周期和历史查询编排。
- `frontend/src/components/data-trunk/NodeTrunkOverview.tsx`：L0/L2 同帧展示。
- `frontend/src/components/data-trunk/EntityObservationCard.tsx`：L2 帧、质量和来源字段展示。

---

### Task 1: CommittedFrameStream 领域契约与内存行为

**Files:**
- Create: `backend/app/services/committed_frame_stream.py`
- Create: `backend/tests/test_committed_frame_stream.py`

**Interfaces:**
- Consumes: `FrameOutboxEvent`、`TypedValue`、`TrunkQuality`、`FrameStatus` from `app.services.data_trunk_contracts/data_trunk_outbox`。
- Produces: `FrameScope.for_node(node_id)`、`FrameCursorCodec.encode/decode`、`FrameDelta.from_event(event,scope)`、`FrameDelta.public_dict()`、`CommittedFrameStream.read_snapshot(scope)`、`CommittedFrameStream.subscribe_after(scope,cursor)`、`CommittedFrameStream.publish(event)`。

- [ ] **Step 1: 写游标、快照、重复帧和过旧游标 RED 测试**

```python
class CommittedFrameStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_cursor_is_bound_to_node_scope(self):
        snapshot = stream.read_snapshot(FrameScope.for_node(NODE_A))
        with self.assertRaisesRegex(FrameStreamError, "FRAME_CURSOR_SCOPE_MISMATCH"):
            codec.decode(snapshot.cursor, FrameScope.for_node(NODE_B))

    async def test_replay_then_live_is_ordered_and_deduplicated(self):
        subscription = await stream.subscribe_after(scope, cursor_for(10))
        await stream.publish(event(12))
        self.assertEqual([11, 12], [
            (await subscription.receive()).frame_sequence,
            (await subscription.receive()).frame_sequence,
        ])

    async def test_cursor_older_than_replay_horizon_requires_snapshot(self):
        with self.assertRaisesRegex(FrameStreamError, "FRAME_CURSOR_TOO_OLD"):
            await stream.subscribe_after(scope, cursor_for(3))
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest tests.test_committed_frame_stream -v`（workdir: `backend`）

Expected: FAIL，`app.services.committed_frame_stream` 尚不存在。

- [ ] **Step 3: 实现最小领域类型和深模块**

```text
FrameScope.for_node(node_id: UUID) -> FrameScope
FrameCursorCodec.encode(sequence: int, scope: FrameScope) -> str
FrameCursorCodec.decode(value: str, scope: FrameScope) -> int
FrameStreamRepository.read_snapshot(scope: FrameScope) -> FrameSnapshot
FrameStreamRepository.replay_window() -> ReplayWindow
FrameStreamRepository.replay_after(sequence: int, high_watermark: int, scope: FrameScope) -> Sequence[FrameDelta]
CommittedFrameStream.read_snapshot(scope: FrameScope) -> FrameSnapshot
CommittedFrameStream.subscribe_after(scope: FrameScope, cursor: str) -> FrameSubscription
async CommittedFrameStream.publish(event: FrameOutboxEvent) -> None
```

游标规范 JSON 固定为 `{"v":1,"s":frame_sequence,"scope":sha256("node:"+uuid)}` 后 URL-safe base64；decode 拒绝版本、负序号、scope 不匹配和畸形输入。`FrameDelta.from_event` 根据每个 change 的 `node_id` 过滤，但即使结果为空也保留帧头作为 checkpoint。`subscribe_after` 先注册 live buffer，再捕获 repository 高水位、读取 durable replay、合并 buffer，按序号去重。`FrameSubscription` 内部使用 `asyncio.Queue(maxsize=64)`，满时产生 `FRAME_CLIENT_TOO_SLOW`；dispatcher 以 `CommittedFrameStream.publish(event)` 作为真实 consumer seam。

- [ ] **Step 4: 跑纯契约测试**

Run: `python -m unittest tests.test_committed_frame_stream -v`

Expected: 全部 PASS，覆盖 64 帧溢出、checkpoint、FAILED、scope 过滤和重复序号。

- [ ] **Step 5: 提交 Task 1**

```bash
git add backend/app/services/committed_frame_stream.py backend/tests/test_committed_frame_stream.py
git commit -m "feat: define committed frame stream"
```

### Task 2: Schema 047 不可变数据帧 delta payload

**Files:**
- Create: `init-db/migration_047_committed_frame_payload.sql`
- Create: `backend/tests/test_committed_frame_payload_migration_postgres.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/app/services/data_trunk_outbox.py`
- Modify: `backend/tests/test_data_frames_postgres.py`
- Modify: `backend/tests/test_data_frame_outbox.py`

**Interfaces:**
- Consumes: Task 1 `FrameDelta.public_dict()` 的版本 1 wire shape。
- Produces: `t_data_frame_outbox(payload_version SMALLINT, payload JSONB)`；`PostgresFrameOutboxRepository` 从单行 payload 构造 `FrameOutboxEvent`，不回扫历史表。

- [ ] **Step 1: 写 046→047/fresh/replay/非空旧 outbox 拒绝 RED 测试**

```python
def test_047_rejects_existing_outbox_before_any_ddl(self):
    self.insert_terminal_outbox_without_payload()
    with self.assertRaisesRegex(Exception, "SCHEMA_047_OUTBOX_NOT_EMPTY"):
        self.apply_047()
    self.assertColumnMissing("t_data_frame_outbox", "payload_version")

def test_transaction_b_writes_one_versioned_delta_payload(self):
    terminal = repository.complete_frame(claim, outputs, now=NOW)
    row = self.fetch_one("SELECT payload_version,payload FROM t_data_frame_outbox")
    self.assertEqual(1, row[0])
    self.assertEqual(terminal.frame_sequence, row[1]["frame_sequence"])
```

- [ ] **Step 2: 运行 PostgreSQL RED**

Run: `python tests/run_postgres_group.py tests.test_committed_frame_payload_migration_postgres tests.test_data_frames_postgres`（workdir: `backend`）

Expected: FAIL，Migration 047 和 payload 列不存在。

- [ ] **Step 3: 实现 Schema 047 和事务 B payload**

Migration 先持有 `zizu-schema-047` advisory transaction lock，在任何 DDL 前验证完整 046 且 outbox 为零行；随后增加：

```sql
ALTER TABLE public.t_data_frame_outbox
  ADD COLUMN payload_version SMALLINT NOT NULL DEFAULT 1
    CHECK (payload_version = 1),
  ADD COLUMN payload JSONB NOT NULL;
ALTER TABLE public.t_data_frame_outbox
  ALTER COLUMN payload_version DROP DEFAULT;
CREATE INDEX ix_data_frame_outbox_replay
  ON public.t_data_frame_outbox(frame_sequence)
  WHERE published_at IS NOT NULL;
```

事务 B 在插入 outbox 前构造一个规范 payload：帧头、完整 `l0_changes`、完整 `l2_changes`、失败事实。每个 change 必须保存归属 `node_id`，让 live/replay 无数据库旁查即可按 scope 过滤；类型化值只能出现一个 `value` 字段和一个明确 `data_type`，时间统一 UTC ISO-8601。修改 `PostgresFrameOutboxRepository._load_event` 直接解码 payload，并删除逐帧 `_load_l0_state`/previous-frame diff 查询。

- [ ] **Step 4: 验证 payload 不可变和 dispatcher 行为**

Run: `python -m unittest tests.test_data_frame_outbox -v`

Run: `python tests/run_postgres_group.py tests.test_committed_frame_payload_migration_postgres tests.test_data_frames_postgres`

Expected: 全部 PASS、0 skip；重试读取完全相同 payload，FAILED payload 含 failure code，事务 B fault hook 回滚时无 outbox。

- [ ] **Step 5: 提交 Task 2**

```bash
git add init-db/migration_047_committed_frame_payload.sql backend/app/services/data_trunk_postgres.py backend/app/services/data_trunk_outbox.py backend/tests/test_committed_frame_payload_migration_postgres.py backend/tests/test_data_frames_postgres.py backend/tests/test_data_frame_outbox.py
git commit -m "feat: persist committed frame deltas"
```

### Task 3: PostgreSQL 快照与 durable replay adapter

**Files:**
- Create: `backend/app/services/committed_frame_stream_postgres.py`
- Create: `backend/tests/test_committed_frame_stream_postgres.py`

**Interfaces:**
- Consumes: Task 1 `FrameStreamRepository`、Task 2 outbox payload。
- Produces: `PostgresCommittedFrameStreamRepository.read_snapshot/replay_window/replay_after`。

- [ ] **Step 1: 写一致快照、节点隔离和 horizon RED 测试**

```python
def test_snapshot_uses_one_terminal_database_cut(self):
    before = self.commit_frame(node=A, sequence=7)
    repository.on_snapshot_head_read = lambda: self.commit_frame(node=A, sequence=8)
    snapshot = repository.read_snapshot(FrameScope.for_node(A))
    self.assertEqual(7, snapshot.frame_sequence)
    self.assertTrue(all(item.frame_sequence <= 7 for item in (*snapshot.l0, *snapshot.l2)))

def test_replay_filters_payload_without_leaking_other_node(self):
    rows = repository.replay_after(7, 9, FrameScope.for_node(A))
    self.assertEqual({TAG_A}, {item.tag_id for row in rows for item in row.l0_changes})
```

- [ ] **Step 2: 运行 PostgreSQL RED**

Run: `python tests/run_postgres_group.py tests.test_committed_frame_stream_postgres`（workdir: `backend`）

Expected: FAIL，PostgreSQL adapter 尚不存在。

- [ ] **Step 3: 实现一致快照和批量 replay**

`read_snapshot` 使用一个 `BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY` 切面，先验证活动节点，再读取最大 COMPLETE/FAILED 帧号，批量读取该节点活动 tags、`t_telemetry_latest`、活动 entity instances 与 `t_l2_latest`。无观测项返回空值和 STALE 等待状态。replay 使用单条有界 SQL 读取 `(after, high_watermark]` 的 payload，再按同一事务解析出的 tag/entity 身份集合过滤；不为每帧执行查询。

```python
def replay_after(self, sequence, high_watermark, scope):
    if high_watermark - sequence > 5000:
        raise FrameStreamError("FRAME_CURSOR_TOO_OLD")
    # one ordered SELECT from t_data_frame_outbox, then in-memory scope filter
```

- [ ] **Step 4: 跑 adapter 与既有数据帧 PostgreSQL 回归**

Run: `python tests/run_postgres_group.py tests.test_committed_frame_stream_postgres tests.test_data_frames_postgres`

Expected: 全部 PASS、0 skip；查询计数证明 snapshot 为固定批量查询、replay 为单次有界读取。

- [ ] **Step 5: 提交 Task 3**

```bash
git add backend/app/services/committed_frame_stream_postgres.py backend/tests/test_committed_frame_stream_postgres.py
git commit -m "feat: read committed frame snapshots"
```

### Task 4: 统一 REST/WebSocket 与真实 dispatcher consumer

**Files:**
- Create: `backend/app/api/committed_frames.py`
- Create: `backend/tests/test_committed_frame_public_api.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/services/pipeline.py`
- Modify: `backend/tests/test_data_trunk_startup_gate.py`

**Interfaces:**
- Consumes: Task 1 `CommittedFrameStream/FrameHub`、Task 3 PostgreSQL adapter、现有 `/auth/ws-ticket` 与 `runtime.read`。
- Produces: `GET /api/v1/runtime/frame-snapshot`、`WS /api/v1/ws/data-frames`，以及 lifespan 中名为 `data_frame_outbox` 的 dispatcher loop。

- [ ] **Step 1: 写 HTTP/WS 安全、补帧与慢客户端 RED 测试**

```python
def test_snapshot_requires_runtime_read_and_returns_cursor(self):
    response = client.get(f"/api/v1/runtime/frame-snapshot?node_id={NODE_ID}", headers=runtime_headers())
    self.assertEqual(200, response.status_code)
    self.assertEqual("frame_snapshot", response.json()["type"])

def test_websocket_replays_after_authenticated_cursor(self):
    with client.websocket_connect("/api/v1/ws/data-frames") as socket:
        socket.send_json({"authenticate": {"ticket": ticket}})
        socket.send_json({"subscribe": {"node_id": str(NODE_ID), "after": cursor}})
        self.assertEqual(11, socket.receive_json()["frame_sequence"])
```

- [ ] **Step 2: 运行 API RED**

Run: `python -m unittest tests.test_committed_frame_public_api -v`（workdir: `backend`）

Expected: FAIL，新 router 尚不存在。

- [ ] **Step 3: 实现 router、依赖注入和 dispatcher loop**

REST 将 `FrameStreamError` 映射为稳定 400/404/409 包络。WebSocket 在 accept 后五秒内要求单次 ticket，重验 `runtime.read`，再验证 node scope；`FRAME_CURSOR_TOO_OLD` 发送 `resnapshot_required` 后关闭。lifespan 在 capture/processor 之外启动第三个有界 loop：

```python
async def _data_frame_outbox_loop() -> None:
    while not stop.is_set():
        published = await dispatcher.run_once()
        if published == 0:
            await wait_or_stop(0.25)
```

构造真实 consumer 后调用 `runtime.configuration_gate.register_committed_frame_consumer()`；只有 hub 接受完整事件后 dispatcher 才 mark published。shutdown 先停止 dispatcher，再关闭 hub 和数据库池。

- [ ] **Step 4: 跑 API、身份和启动回归**

Run: `python -m unittest tests.test_committed_frame_public_api tests.test_control_management_ws_security tests.test_data_trunk_startup_gate -v`

Expected: 全部 PASS；未认证 4401、无权限 4403、非安全传输规则不回归、配置发布不再报 consumer missing。

- [ ] **Step 5: 提交 Task 4**

```bash
git add backend/app/api/committed_frames.py backend/app/main.py backend/app/services/pipeline.py backend/tests/test_committed_frame_public_api.py backend/tests/test_data_trunk_startup_gate.py
git commit -m "feat: stream committed data frames"
```

### Task 5: Schema 048 固定 L2/帧/outbox 保留

**Files:**
- Create: `init-db/migration_048_frame_retention.sql`
- Create: `backend/tests/test_frame_retention_migration_postgres.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/tests/test_data_trunk_startup_gate.py`
- Modify: `scripts/test_build_release_images.py`

**Interfaces:**
- Consumes: Schema 047 payload/outbox、Schema 045 L0 retention。
- Produces: `l2_agg_1h`、`l2_agg_1d`、`prune_committed_frame_history(integer,jsonb)`、固定 maintenance job 和完整 Schema 048 startup gate。

- [ ] **Step 1: 写 fresh/upgrade/replay/引用保护与过期清理 RED 测试**

```python
def test_048_prunes_only_published_outbox_beyond_hour_or_5000(self):
    self.insert_published_outbox(sequence=1, age=timedelta(minutes=61))
    self.insert_published_outbox(sequence=2, age=timedelta(minutes=59))
    self.call_prune(now=NOW)
    self.assertEqual([2], self.outbox_sequences())

def test_048_never_prunes_unpublished_or_claimed_outbox(self):
    self.insert_unpublished_outbox(sequence=1, age=timedelta(days=8), claimed=True)
    self.call_prune(now=NOW)
    self.assertEqual([1], self.outbox_sequences())

def test_048_preserves_frame_referenced_by_failure_or_audit(self):
    frame_id = self.insert_old_failed_frame(age=timedelta(days=8))
    self.call_prune(now=NOW)
    self.assertTrue(self.frame_exists(frame_id))

def test_048_keeps_latest_projection_while_raw_history_expires(self):
    entity_id = self.insert_old_l2_with_latest(age=timedelta(days=8))
    self.call_prune(now=NOW)
    self.assertIsNotNone(self.read_l2_latest(entity_id))

def test_048_materializes_numeric_and_discrete_l2_aggregates(self):
    self.insert_l2_aggregate_examples()
    self.refresh_l2_aggregates()
    self.assertEqual((10.0, 20.0, 15.0), self.numeric_min_max_avg())
    self.assertEqual(("OFF", "ON"), self.discrete_first_last())
```

每个测试插入明确的 6 天/8 天/61 分钟边界数据，并在调用维护函数前后核对 L2 observations、sources、frames、outbox、latest 和汇总行数。

- [ ] **Step 2: 运行 Schema 048 RED**

Run: `python tests/run_postgres_group.py tests.test_frame_retention_migration_postgres`（workdir: `backend`）

Expected: FAIL，Migration 048 和固定 job 不存在。

- [ ] **Step 3: 实现汇总与受控维护路径**

`l2_agg_1h` 与 `l2_agg_1d` 都固定输出：`bucket`、`entity_instance_id`、`sample_count`、`good_count`、`uncertain_count`、`bad_count`、`stale_count`、数值 `first/last/min/max/avg`，以及布尔、文本、代码数组的 `first/last`。两张连续聚合都直接从 raw L2 构造，避免级联连续聚合限制。维护函数使用 transaction advisory lock，按以下顺序执行：

```text
删除已发布且 created_at < now()-1h 或不在最新 5000 的 outbox
删除到期且无长期证据引用的 L2 source rows
删除到期且无长期证据引用的 L2 observations
删除已无 telemetry/L2/failure/audit 引用且 finished_at < now()-7d 的终态 frames
```

函数使用固定 `search_path=pg_catalog,public`、SECURITY DEFINER、撤销 PUBLIC EXECUTE，并由唯一每小时 Timescale job 调用。普通 DELETE/TRUNCATE 仍被 append-only/terminal trigger 拒绝；维护函数仅通过 transaction-local maintenance flag 打开精确删除窗口，异常时 flag 随事务回滚。

- [ ] **Step 4: 收紧 startup/release gate 并跑存储大组**

`verify_data_trunk_contract_gate` 增加 payload 列、replay index、048 聚合、job、函数 owner/search_path/ACL/trigger 指纹检查。`data_frame_release_readiness_blockers(committed_frame_consumer=True, retention_policy_resolved=True)` 必须为空；更新 release build test 的最新 schema 期望为 `048`。

Run: `python tests/run_postgres_group.py tests.test_frame_retention_migration_postgres tests.test_committed_frame_payload_migration_postgres tests.test_data_frames_migration_postgres tests.test_edge_storage_retention_migration_postgres`

Expected: 全部 PASS、0 skip。

- [ ] **Step 5: 提交 Task 5**

```bash
git add init-db/migration_048_frame_retention.sql backend/app/services/data_trunk_postgres.py backend/tests/test_frame_retention_migration_postgres.py backend/tests/test_data_trunk_startup_gate.py scripts/test_build_release_images.py
git commit -m "feat: bound committed frame retention"
```

### Task 6: 前端快照/增量客户端与纯投影状态机

**Files:**
- Create: `frontend/src/api/committedFrameStream.ts`
- Create: `frontend/src/components/data-trunk/committedFrameProjection.ts`
- Create: `frontend/src/components/data-trunk/committedFrameProjection.test.mjs`
- Modify: `frontend/src/api/client.ts`

**Interfaces:**
- Consumes: Task 4 wire contract、现有 `apiFetch` 和 WebSocket ticket。
- Produces: `fetchCommittedFrameSnapshot(nodeId)`、`connectCommittedFrameStream(options)`、`replaceSnapshot`、`applyFrameDelta`。

- [ ] **Step 1: 写一次替换、原子增量、去重和重读 RED 测试**

```javascript
test('one frame applies l0 and l2 atomically and rejects duplicates', async () => {
  const projection = await import('./committedFrameProjection.ts')
  const initial = projection.replaceSnapshot(null, snapshot(10))
  const next = projection.applyFrameDelta(initial, delta(11))
  assert.equal(next.frameSequence, 11)
  assert.equal(next.l0.get('tag-a').frame_sequence, 11)
  assert.equal(next.l2.get('entity-a').frame_sequence, 11)
  assert.strictEqual(projection.applyFrameDelta(next, delta(11)), next)
})
```

- [ ] **Step 2: 运行前端 RED**

Run: `node --test src/components/data-trunk/committedFrameProjection.test.mjs`（workdir: `frontend`）

Expected: FAIL，投影模块不存在。

- [ ] **Step 3: 实现类型、纯 reducer 和客户端连接**

从 `client.ts` 导出既有 `apiFetch`，新客户端不得复制认证逻辑。`connectCommittedFrameStream` 获取 ticket、认证、发送 node/cursor，维护最后成功应用游标，普通断线按 1/2/3/4/5 秒重连；收到 `resnapshot_required` 只调用一次 `onResnapshotRequired`，由上层重新读快照。使用 generation token 防止已取消连接回调。

```typescript
export interface CommittedFrameProjection {
  nodeId: string
  cursor: string
  frameSequence: number
  l0: Map<string, L0FrameItem>
  l2: Map<string, L2FrameItem>
}
```

- [ ] **Step 4: 跑投影测试和类型检查**

Run: `node --test src/components/data-trunk/committedFrameProjection.test.mjs && npx tsc -b`（workdir: `frontend`）

Expected: 全部 PASS、TypeScript exit 0。

- [ ] **Step 5: 提交 Task 6**

```bash
git add frontend/src/api/client.ts frontend/src/api/committedFrameStream.ts frontend/src/components/data-trunk/committedFrameProjection.ts frontend/src/components/data-trunk/committedFrameProjection.test.mjs
git commit -m "feat: consume committed frame snapshots"
```

### Task 7: 节点 L0/L2 界面接入并硬删旧双流

**Files:**
- Modify: `frontend/src/components/data-trunk/DataTrunkWorkspace.tsx`
- Modify: `frontend/src/components/data-trunk/NodeTrunkOverview.tsx`
- Modify: `frontend/src/components/data-trunk/EntityObservationCard.tsx`
- Modify: `frontend/src/components/NodeTagPanel.tsx`
- Delete: `frontend/src/components/NodeRealtimePanel.tsx`
- Modify: `frontend/src/api/client.ts`
- Delete: `backend/app/api/websocket.py`
- Modify: `backend/app/main.py`
- Delete: `backend/tests/test_entity_observation_websocket.py`
- Modify: `backend/tests/test_control_management_ws_security.py`
- Create: `backend/tests/test_realtime_hard_cut.py`

**Interfaces:**
- Consumes: Task 6 projection/client、Task 4 unified router。
- Produces: 节点页唯一 L0/L2 实时路径；源码中不再存在旧 endpoint、poller 或客户端函数。

- [ ] **Step 1: 写硬切和界面字段 RED 契约**

```python
def test_only_committed_frame_realtime_route_remains(self):
    source = read_runtime_sources()
    self.assertNotIn("/ws/telemetry", source)
    self.assertNotIn("/ws/entity-observations", source)
    self.assertNotIn("POLL_INTERVAL = 1.5", source)
    self.assertIn("/ws/data-frames", source)
```

前端静态契约同时要求出现 value、unit、quality、source timestamp、received time、frame time、frame sequence、configuration revision、L0 source 和 L2 source evidence。

- [ ] **Step 2: 运行硬切 RED**

Run: `python -m unittest tests.test_realtime_hard_cut -v`（workdir: `backend`）

Expected: FAIL，旧双流仍存在。

- [ ] **Step 3: 接入 Workspace 并一次应用同帧 L0/L2**

`DataTrunkWorkspace` 在 node generation 内先并行读取静态 trunk 描述、实体 catalog、历史趋势和 frame snapshot；以 snapshot 一次替换投影，再携带游标连接 unified WS。节点切换时先 abort 旧 snapshot、关闭旧 socket、清空投影。`NodeTrunkOverview` 用 descriptor + projection map 展示 L0/L2；STALE 保留值但灰显，不再用“值非空”推断在线。

`NodeTagPanel` 删除 `connectTelemetryWS`，保留配置查询/手动刷新；`NodeRealtimePanel.tsx` 没有调用者且代表旧旁路，按已确认规格删除。删除旧 backend websocket router/test 和旧 frontend `connectTelemetryWS/connectEntityObservationWS`；`main.py` 只注册 `committed_frames.router`。

- [ ] **Step 4: 跑后端硬切、前端状态和生产构建**

Run: `python -m unittest tests.test_realtime_hard_cut tests.test_committed_frame_public_api tests.test_control_management_ws_security -v`（workdir: `backend`）

Run: `node --test src/components/data-trunk/committedFrameProjection.test.mjs src/components/data-trunk/dataTrunkViewModel.test.mjs && npm run build`（workdir: `frontend`）

Expected: 全部 PASS；Vite exit 0；静态扫描无旧 endpoint/function/import。

- [ ] **Step 5: 提交 Task 7**

```bash
git add -A backend/app/api backend/app/main.py backend/tests frontend/src/api frontend/src/components
git commit -m "feat: show atomic l0 and l2 frames"
```

### Task 8: 全量门禁、文档与交付边界

**Files:**
- Modify: `README.md`
- Modify: `CODEX_HANDOFF.md`
- Modify: `docs/superpowers/specs/2026-08-27-committed-frame-realtime-stream-design.md`

**Interfaces:**
- Consumes: Tasks 1–7 完整实现。
- Produces: Accepted/Implemented 规格状态、可复现验证命令和明确“未部署”交接。

- [ ] **Step 1: 更新文档和规格状态**

README 只记录唯一实时读取：`GET /api/v1/runtime/frame-snapshot` + `WS /api/v1/ws/data-frames`；删除旧双流说明。规格 front matter 从 `accepted` 改为 `implemented`，附实现提交和 Schema 048。Handoff 写清测试结果、删除文件理由、未构建镜像、未连接 1 号机。

- [ ] **Step 2: 跑完整后端与脚本回归**

Run: `python -m unittest discover -s tests -p "test_*.py"`（workdir: `backend`）

Run: `python -m unittest discover -s scripts -p "test_*.py"`（workdir: repository root）

Expected: 无失败；仅仓库既有显式环境 skip。

- [ ] **Step 3: 跑真实 PostgreSQL 数据帧/迁移/保留大组**

Run: `python tests/run_postgres_group.py tests.test_committed_frame_payload_migration_postgres tests.test_frame_retention_migration_postgres tests.test_committed_frame_stream_postgres tests.test_data_frames_migration_postgres tests.test_data_frames_postgres tests.test_data_frame_acceptance_postgres tests.test_edge_storage_retention_migration_postgres`（workdir: `backend`）

Expected: 全部 PASS、0 skip。使用名称和数据库名都带 `_test` 的一次性 PostgreSQL 16 + TimescaleDB 2.29.2 容器；完成后按精确名称删除容器及匿名卷，不触碰现场或已有开发数据库。

- [ ] **Step 4: 跑前端和静态最终门禁**

Run: `node --test src/components/data-trunk/*.test.mjs && npm run build`（workdir: `frontend`）

Run: `python -m compileall -q app`（workdir: `backend`）

Run: `git diff --check && rg -n "/ws/telemetry|/ws/entity-observations|connectTelemetryWS|connectEntityObservationWS|POLL_INTERVAL = 1.5" backend frontend`

Expected: 测试/build/compileall/diff 全部 exit 0；最后 `rg` 无匹配并以 exit 1 表示旧路径已不存在。

- [ ] **Step 5: 最终提交**

```bash
git add README.md CODEX_HANDOFF.md docs/superpowers/specs/2026-08-27-committed-frame-realtime-stream-design.md
git commit -m "docs: record committed frame stream delivery"
```

- [ ] **Step 6: 记录边界**

确认 `git status --short` 为空。最终报告只宣称本地实现与验证完成；不宣称镜像已发布或 1 号机已部署。下一阶段单独规划告警/JDM/控制/画面接入 committed L2。
