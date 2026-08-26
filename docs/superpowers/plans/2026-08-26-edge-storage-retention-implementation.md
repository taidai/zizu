# Edge Storage Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 1 号机无限增长的数据改成有明确上限、可追溯、可回滚的固定保留方案，并发布 v0.4.85-rc.4 / Schema 045。

**Architecture:** `t_l0_observation_dedup` 只承担 6 小时防重复缓存，L0/L2 历史自行保存来源证据。Schema 045 负责紧凑缓存表、安装 TimescaleDB 原生清理/压缩/保留/聚合任务；应用写入对超期重放使用历史唯一约束兜底。发布继续使用固定 ARM64 镜像摘要，现场只停止 backend，保留 Neuron、NanoMQ、TimescaleDB 和设备运行。

**Tech Stack:** Python 3.12、FastAPI、psycopg2、PostgreSQL 16、TimescaleDB、Docker、GitHub Actions、React/Vite

**Spec:** `docs/superpowers/specs/2026-08-26-edge-storage-retention-design.md`

## Global Constraints

- 防重复缓存固定保留 6 小时，每 15 分钟清理一次。
- L0 明细使用 1 小时 chunk，6 小时后压缩，7 天后删除。
- `tel_agg_1h` 每小时刷新、`tel_agg_1d` 每日刷新并长期保留；`tel_agg_5min` 本轮不增加任务也不删除。
- 不增加策略中心、应用层调度器、公开接口或新依赖。
- 不改变 L1、L2、告警、JDM、控制和画面语义。
- 只部署固定 `linux/arm64` 镜像摘要，不使用 `latest`，不启动 Caddy/TLS。
- 现场维护只停止 backend，不发送设备控制，不验证自动策略，允许留下明确的 5～15 分钟数据空档。
- 删除任何现场文件前必须先验证 resolved absolute path、备份 SHA 和恢复可读性；禁止 `docker system prune`、`docker volume prune` 及未解析 glob。

---

## File Map

- `backend/app/services/data_trunk_postgres.py`：L0 批量入库；让历史唯一约束成为缓存过期后的第二道防重门。
- `backend/tests/test_data_trunk_bulk_write.py`：锁定单批 SQL 和只返回真正写入历史的 observation。
- `backend/tests/test_edge_storage_retention_migration_postgres.py`：Schema 045 的真实 TimescaleDB fresh、upgrade、replay、损坏拒绝、任务与数据保留测试。
- `init-db/migration_045_edge_storage_retention.sql`：紧凑防重缓存、解开历史 FK、安装固定保留与聚合任务。
- `scripts/test_build_release_images.py`：把发布清单的最高 Schema 期望从 044 提升到 045。
- `VERSION`、`backend/app/VERSION`、`backend/pyproject.toml`、`frontend/package.json`、`frontend/package-lock.json`、`README.md`：统一发布身份为 `0.4.85-rc.4`。
- `docs/deploy-1号机-v0.4.85-rc.4-http.md`：记录真实提交、Actions run、镜像摘要、备份 SHA、清理路径、迁移结果与现场验收。
- `CODEX_HANDOFF.md`：记录每个开发门禁与最终现场状态。

---

### Task 1: 缓存过期后的 L0 重放仍保持幂等

**Files:**
- Modify: `backend/tests/test_data_trunk_bulk_write.py`
- Modify: `backend/app/services/data_trunk_postgres.py:783-856`

**Interfaces:**
- Consumes: `PostgresDataTrunkRepository._insert_l0(cursor, observations)` 和既有唯一索引 `uq_telemetry_source_observation(tag_id, ts, source_digest)`。
- Produces: `_insert_l0(cursor, observations)` 返回 `RawObservation` 元组，且只包含真正插入 `t_telemetry` 的 observation；缓存命中和历史冲突都计为 duplicate。

- [ ] **Step 1: 写入会失败的批量 SQL 合约测试**

在 `DataTrunkBulkWriteTest` 增加捕获 `execute_values` SQL 的测试：

```python
def test_l0_history_conflict_returns_only_rows_inserted_into_history(self) -> None:
    observations = (_observation(1), _observation(2))
    captured: dict[str, str] = {}

    def execute_values_contract(_cursor, statement, _rows, **_kwargs):
        captured["sql"] = statement
        return [(str(observations[1].observation_id),)]

    with patch(
        "app.services.data_trunk_postgres.execute_values",
        side_effect=execute_values_contract,
    ):
        accepted = PostgresDataTrunkRepository._insert_l0(
            _RejectPerRowCursor(), observations
        )

    normalized = " ".join(captured["sql"].split())
    self.assertIn(
        "ON CONFLICT (tag_id, ts, source_digest) "
        "WHERE source_digest IS NOT NULL DO NOTHING",
        normalized,
    )
    self.assertEqual((observations[1],), accepted)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
Set-Location backend
python -m unittest tests.test_data_trunk_bulk_write.DataTrunkBulkWriteTest.test_l0_history_conflict_returns_only_rows_inserted_into_history -v
```

Expected: FAIL；现有 SQL 在 `INSERT INTO t_telemetry` 后没有历史冲突处理。

- [ ] **Step 3: 在历史插入后增加精确冲突目标**

把 `_insert_l0` 的最终插入结尾改为：

```sql
FROM input
JOIN accepted USING (observation_id)
ON CONFLICT (tag_id, ts, source_digest)
WHERE source_digest IS NOT NULL
DO NOTHING
RETURNING observation_id
```

不要改动缓存的 `ON CONFLICT (source_digest) DO NOTHING`。函数继续用最终 `RETURNING` 构造 `accepted_ids`，因此新插入缓存、但被历史唯一索引拒绝的超期重放不会进入 latest、L1 或 L2。

- [ ] **Step 4: 运行批量写入回归**

Run:

```powershell
Set-Location backend
python -m unittest tests.test_data_trunk_bulk_write -v
```

Expected: 5 tests PASS；仍保持一次历史数据库调用和 latest 有界调用。

- [ ] **Step 5: 提交 Task 1**

```powershell
git add backend/app/services/data_trunk_postgres.py backend/tests/test_data_trunk_bulk_write.py
git commit -m "fix(data-trunk): deduplicate expired L0 replays"
```

---

### Task 2: Schema 045 分离防重复缓存与来源证据

**Files:**
- Create: `backend/tests/test_edge_storage_retention_migration_postgres.py`
- Create: `init-db/migration_045_edge_storage_retention.sql`

**Interfaces:**
- Consumes: 完整 Schema 044、`t_telemetry`、`t_telemetry_latest`、`t_l2_observation_sources`、三个既有 CAGG。
- Produces: `public.prune_l0_observation_dedup(integer,jsonb)`、6 小时防重 job、1h/1d refresh job、1 小时 telemetry chunk、6 小时压缩和 7 天 retention。

- [ ] **Step 1: 建立真实 TimescaleDB 测试骨架**

测试文件固定读取迁移：

```python
MIGRATION_045 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_045_edge_storage_retention.sql"
)

@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run edge retention migration tests",
)
class EdgeStorageRetentionMigrationPostgresTest(unittest.TestCase):
    @staticmethod
    def _apply_045(cursor) -> None:
        cursor.execute(MIGRATION_045.read_text(encoding="utf-8"))
```

`setUpClass` 复用现有安全门：`DB_NAME` 必须以 `_test` 结尾；每个测试先调用 `NodeDataTrunkHardCutMigrationPostgresTest._reset_through_043(cursor)` 和 `_apply_044(cursor)`。

- [ ] **Step 2: 写 upgrade 数据职责测试并确认 RED**

测试插入两个防重记录：一个 `created_at=now()-interval '7 hours'`，一个 `created_at=now()-interval '5 hours'`；同时为旧记录插入可追溯的 `t_telemetry` 和 `t_l2_observation_sources` 事实。应用 045 后断言：

```python
cursor.execute(
    "SELECT source_digest FROM t_l0_observation_dedup ORDER BY source_digest"
)
self.assertEqual(["b" * 64], [row[0].strip() for row in cursor.fetchall()])

cursor.execute(
    "SELECT observation_id, source_digest FROM t_telemetry "
    "WHERE source_digest=%s",
    ("a" * 64,),
)
self.assertEqual((old_observation_id, "a" * 64), cursor.fetchone())

cursor.execute(
    "SELECT l0_observation_id, source_digest FROM t_l2_observation_sources "
    "WHERE source_digest=%s",
    ("a" * 64,),
)
self.assertEqual((old_observation_id, "a" * 64), cursor.fetchone())
```

再查询 `pg_constraint`，断言三处指向 `t_l0_observation_dedup` 的 FK 均为 0，但原有 typed-value CHECK 仍存在。

Run:

```powershell
Set-Location backend
$env:ZIZU_POSTGRES_TEST='1'
python -m unittest tests.test_edge_storage_retention_migration_postgres.EdgeStorageRetentionMigrationPostgresTest.test_045_compacts_cache_without_deleting_history_evidence -v
```

Expected: ERROR；迁移文件尚不存在。

- [ ] **Step 3: 写 migration preflight、外键拆分与紧凑表替换**

迁移以 `BEGIN` 开始，先验证 Schema 044 所需对象全部存在。实际切换采用同一事务内的表替换：

```sql
ALTER TABLE public.t_telemetry
  DROP CONSTRAINT IF EXISTS fk_telemetry_l0_observation;
ALTER TABLE public.t_telemetry_latest
  DROP CONSTRAINT IF EXISTS fk_telemetry_latest_l0_observation;
ALTER TABLE public.t_l2_observation_sources
  DROP CONSTRAINT IF EXISTS t_l2_observation_sources_l0_observation_id_fkey;

ALTER TABLE public.t_l0_observation_dedup
  RENAME TO t_l0_observation_dedup_044_retired;

CREATE TABLE public.t_l0_observation_dedup (
  observation_id UUID PRIMARY KEY,
  tag_id UUID NOT NULL REFERENCES public.t_tags(id),
  observed_at TIMESTAMPTZ NOT NULL,
  source_digest CHAR(64) NOT NULL UNIQUE,
  source_message_id TEXT,
  source_sequence BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.t_l0_observation_dedup
  (observation_id, tag_id, observed_at, source_digest,
   source_message_id, source_sequence, created_at)
SELECT observation_id, tag_id, observed_at, source_digest,
       source_message_id, source_sequence, created_at
FROM public.t_l0_observation_dedup_044_retired
WHERE created_at >= clock_timestamp() - interval '6 hours';

CREATE INDEX idx_l0_observation_dedup_created_at
  ON public.t_l0_observation_dedup(created_at);
DROP TABLE public.t_l0_observation_dedup_044_retired;
```

新缓存表不得恢复 `trg_t_l0_observation_dedup_append_only`；其他 L1/L2 append-only 表不变。迁移末尾 `COMMIT`。为 replay 增加明确 final-footprint 分支：若三处历史 FK 均不存在、`idx_l0_observation_dedup_created_at` 存在且所有任务契约完整，则只验证并返回；出现一半新、一半旧的结构时抛出 `SCHEMA_045_PARTIAL_STRUCTURE` / SQLSTATE `55000`，不得修补损坏结构。

- [ ] **Step 4: 写固定清理 job 测试并确认 RED**

测试从 `timescaledb_information.jobs` 查询 `proc_schema='public' AND proc_name='prune_l0_observation_dedup'`，断言恰好一项且 `schedule_interval='00:15:00'`。直接调用 procedure 后，7 小时行删除、5 小时行保留。

Run:

```powershell
python -m unittest tests.test_edge_storage_retention_migration_postgres.EdgeStorageRetentionMigrationPostgresTest.test_045_installs_one_fixed_six_hour_dedup_job -v
```

Expected: FAIL；procedure 和 job 尚不存在。

- [ ] **Step 5: 实现固定清理 procedure 与唯一 job**

procedure 的保留期写死，不接受可变策略：

```sql
CREATE OR REPLACE PROCEDURE public.prune_l0_observation_dedup(
  job_id INTEGER,
  config JSONB
)
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $procedure$
BEGIN
  DELETE FROM public.t_l0_observation_dedup
  WHERE created_at < clock_timestamp() - interval '6 hours';
END;
$procedure$;

SELECT add_job(
  'public.prune_l0_observation_dedup',
  interval '15 minutes',
  config => '{}'::jsonb,
  if_not_exists => TRUE
);
```

preflight 必须按 `proc_schema/proc_name` 拒绝重复 job；不得创建应用 scheduler、配置表或 API。

- [ ] **Step 6: 写 telemetry/CAGG 策略测试并确认 RED**

测试通过 `timescaledb_information.dimensions` 断言 `time_interval='01:00:00'`；通过 `timescaledb_information.jobs` 断言：

```python
expected = {
    ("policy_compression", "t_telemetry", timedelta(hours=6)),
    ("policy_retention", "t_telemetry", timedelta(days=7)),
    ("policy_refresh_continuous_aggregate", "tel_agg_1h", timedelta(hours=1)),
    ("policy_refresh_continuous_aggregate", "tel_agg_1d", timedelta(days=1)),
}
```

同时断言 `tel_agg_5min` 没有 refresh job。插入最近 48 小时内跨小时和跨日的 float 遥测，调用 1h/1d refresh 后断言两个视图都有行；测试库再执行 `drop_chunks('public.t_telemetry', older_than => clock_timestamp()+interval '1 day')` 删除测试明细，断言 1h/1d 聚合行仍存在。

Run:

```powershell
python -m unittest tests.test_edge_storage_retention_migration_postgres.EdgeStorageRetentionMigrationPostgresTest.test_045_installs_bounded_telemetry_and_long_term_aggregate_jobs -v
```

Expected: FAIL；现有 Schema 没有这些固定策略。

- [ ] **Step 7: 实现 TimescaleDB 原生策略**

先按 hypertable 删除既有 compression/retention policy，再安装唯一固定策略：

```sql
SELECT set_chunk_time_interval('public.t_telemetry', interval '1 hour');
SELECT remove_compression_policy('public.t_telemetry', if_exists => TRUE);
SELECT remove_retention_policy('public.t_telemetry', if_exists => TRUE);
SELECT add_compression_policy(
  'public.t_telemetry', interval '6 hours', if_not_exists => TRUE
);
SELECT add_retention_policy(
  'public.t_telemetry', interval '7 days', if_not_exists => TRUE
);
SELECT add_continuous_aggregate_policy(
  'public.tel_agg_1h',
  start_offset => interval '8 days',
  end_offset => interval '1 hour',
  schedule_interval => interval '1 hour',
  if_not_exists => TRUE
);
SELECT add_continuous_aggregate_policy(
  'public.tel_agg_1d',
  start_offset => interval '8 days',
  end_offset => interval '1 day',
  schedule_interval => interval '1 day',
  if_not_exists => TRUE
);
```

迁移先从 `timescaledb_information.hypertables` 读取 `compression_enabled`；若现场压缩未启用，则执行下列固定设置。若已经启用，只调整 policy，不改写现有压缩参数：

```sql
ALTER TABLE public.t_telemetry SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'node_id,tag_id',
  timescaledb.compress_orderby = 'ts DESC'
);
```

首次安装后调用：

```sql
CALL refresh_continuous_aggregate(
  'public.tel_agg_1h', clock_timestamp() - interval '7 days',
  clock_timestamp() - interval '1 hour'
);
CALL refresh_continuous_aggregate(
  'public.tel_agg_1d', clock_timestamp() - interval '7 days',
  date_trunc('day', clock_timestamp())
);
```

- [ ] **Step 8: 补齐 fresh、replay 与 corruption fail-closed 测试**

增加三个独立测试：

1. 完整 001～044 后首次应用 045，所有对象与任务出现一次；
2. 对完整 045 再执行迁移文本，行数、job IDs 和历史证据不变；
3. 删除 `idx_l0_observation_dedup_created_at` 或额外创建第二个 prune job 后 replay，稳定抛出 `SCHEMA_045_PARTIAL_STRUCTURE`，迁移不产生写入。

Run:

```powershell
python -m unittest tests.test_edge_storage_retention_migration_postgres -v
```

Expected: 全部 PASS，0 skip。

- [ ] **Step 9: 运行真实 L0→L1→L2 超期重放测试**

在同一测试模块创建 `repository = PostgresDataTrunkRepository(clock=lambda: observed_at)` 和 `trunk = DataTrunk(repository)`，写一次 L0，删除其 dedup 缓存，再以相同 `tag_id/source timestamp/source_digest`、不同 observation ID 重放。断言第二次：

```python
self.assertEqual(0, replay.accepted_l0_count)
self.assertEqual(1, replay.duplicate_l0_count)
self.assertEqual((), replay.l2_event_ids)
```

数据库断言相同 digest 的 L0 history、L2 history、outbox 均仍只有第一份。

- [ ] **Step 10: 提交 Task 2**

```powershell
git add init-db/migration_045_edge_storage_retention.sql backend/tests/test_edge_storage_retention_migration_postgres.py
git commit -m "feat(storage): bound edge telemetry retention"
```

---

### Task 3: 发布身份和完整回归

**Files:**
- Modify: `scripts/test_build_release_images.py:55`
- Modify: `VERSION`
- Modify: `backend/app/VERSION`
- Modify: `backend/pyproject.toml:8`
- Modify: `frontend/package.json:4`
- Modify: `frontend/package-lock.json:3,9`
- Modify: `README.md` 中当前版本文本
- Modify: `CODEX_HANDOFF.md`

**Interfaces:**
- Consumes: Task 1/2 的代码、Migration 045 和发布构建器 `_latest_migration_version()`。
- Produces: 单一一致身份 `0.4.85-rc.4` / Schema `045`。

- [ ] **Step 1: 先把发布测试期望改为 Schema 045 并确认 RED**

```python
self.assertEqual("045", release["schema_version"])
```

Run:

```powershell
python -m unittest scripts.test_build_release_images.BuildReleaseImagesTest.test_builds_each_required_architecture_and_writes_a_verified_manifest -v
```

Expected: PASS only after Migration 045 exists；删除或改名 045 时该测试必须回到 FAIL。

- [ ] **Step 2: 统一六处版本文本**

把以下版本全部改为 `0.4.85-rc.4`：

```text
VERSION
backend/app/VERSION
backend/pyproject.toml
frontend/package.json
frontend/package-lock.json（根 version 与 packages[""] version）
README.md 当前版本说明
```

不得运行会升级依赖的 `npm update`；package-lock 只改版本字段。

- [ ] **Step 3: 运行定向后端与脚本门禁**

```powershell
Set-Location backend
python -m unittest tests.test_data_trunk_bulk_write -v
$env:ZIZU_POSTGRES_TEST='1'
python -m unittest tests.test_edge_storage_retention_migration_postgres tests.test_data_trunk_postgres -v
python -m compileall app
Set-Location ..
python -m unittest discover -s scripts -p "test_*.py" -v
```

Expected: 全部 PASS，真实 PostgreSQL 模块 0 skip，compileall 退出 0。

- [ ] **Step 4: 运行完整后端和前端门禁**

```powershell
Set-Location backend
python -m pytest tests -q -p no:cacheprovider
Set-Location ..\frontend
npm run build
Set-Location ..
git diff --check
```

Expected: pytest 0 failed，Vite production build 退出 0，`git diff --check` 无错误。

- [ ] **Step 5: 更新 handoff 并提交发布候选代码**

`CODEX_HANDOFF.md` 记录实际测试数量、测试 TimescaleDB 版本、Migration 045 行为和未触碰 1 号机的事实，然后提交：

```powershell
git add VERSION backend/app/VERSION backend/pyproject.toml frontend/package.json frontend/package-lock.json README.md scripts/test_build_release_images.py CODEX_HANDOFF.md
git commit -m "chore(release): prepare v0.4.85-rc.4"
git status --short
```

Expected: 工作树为空。

---

### Task 4: 固定镜像发布与 1 号机安全切换

**Files:**
- Create: `docs/deploy-1号机-v0.4.85-rc.4-http.md`
- Modify: `CODEX_HANDOFF.md`

**Interfaces:**
- Consumes: 已推送的 rc.4 源码提交、GitHub Actions `release.json`、Schema 044 备份、1 号机现有 host-network/tmpfs 配置。
- Produces: 运行中的固定 ARM64 rc.4 镜像、Schema 045、容量与 10 分钟实时验收证据。

- [ ] **Step 1: 推送源码并构建不可变镜像**

```powershell
git push origin ticket/v0.4.85-node-data-trunk-hard-cut
gh workflow run release-images.yml --ref ticket/v0.4.85-node-data-trunk-hard-cut -f platform_version=0.4.85-rc.4 -f edge_proxy_image=$env:ZIZU_REVIEWED_EDGE_PROXY_IMAGE
gh run list --workflow release-images.yml --branch ticket/v0.4.85-node-data-trunk-hard-cut --limit 1
```

`ZIZU_REVIEWED_EDGE_PROXY_IMAGE` 必须来自 rc.3 已审阅 release manifest；它只满足发布清单校验，本次不部署该代理。等待 run 成功，下载 artifact，执行：

```powershell
python scripts/release_preflight.py verify --release release.json --migrations-dir init-db
```

记录 `linux/arm64` 固定摘要，拒绝 tag 或 `latest`。

- [ ] **Step 2: 现场只读 preflight 和精确清理清单**

先记录：`df -h`、`docker system df -v`、容器配置、Schema、TimescaleDB 版本、`t_l0_observation_dedup`/`t_telemetry` 大小、现有 jobs。列出 `/home/omnithings/bak` 与 `/opt/zizu-backups` 下每个候选文件的 resolved absolute path、大小、时间和 SHA；把实际清单写入 rc.4 部署记录后再执行删除。

保留集合固定为：

- `/opt/zizu-backups/rc7-20260825T080812Z/omnithings.dump` 及其 SHA、runtime env；
- NanoMQ 回滚目录；
- 新建的 `pre-v0.4.85-rc.4-schema044` dump 及其 SHA；
- 当前 rc.3 和上一版 rc.2 固定镜像摘要。

删除集合只允许是已解析的 2026-08-02/03 应用快照、旧应用 tar、经新备份验证后列明的其他旧 DB dump，以及 Docker dangling image ID。发现路径不在 `/home/omnithings/bak` 或 `/opt/zizu-backups`，立即停止。

- [ ] **Step 3: 先释放根盘工作空间并创建可恢复备份**

逐个删除已记录的旧应用快照和旧 tar；对部署记录里的每个 dangling image ID 分别执行一次 `docker image rm` 并保存退出码，不执行任何 broad prune。随后创建 PostgreSQL custom dump：

```bash
install -d -m 0700 /opt/zizu-backups/pre-v0.4.85-rc.4-schema044
PG_IMAGE=$(docker inspect zizu-tsdb --format '{{.Config.Image}}')
docker exec zizu-tsdb sh -ec 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > /opt/zizu-backups/pre-v0.4.85-rc.4-schema044/omnithings.dump
test -s /opt/zizu-backups/pre-v0.4.85-rc.4-schema044/omnithings.dump
sha256sum /opt/zizu-backups/pre-v0.4.85-rc.4-schema044/omnithings.dump > /opt/zizu-backups/pre-v0.4.85-rc.4-schema044/omnithings.dump.sha256
sha256sum --check /opt/zizu-backups/pre-v0.4.85-rc.4-schema044/omnithings.dump.sha256
docker run --rm -v /opt/zizu-backups/pre-v0.4.85-rc.4-schema044:/backup:ro --entrypoint pg_restore "$PG_IMAGE" -l /backup/omnithings.dump >/dev/null
```

以上步骤全部成功后，才逐个删除部署记录中已列明的旧 DB dump。再次记录 `df -h`。

- [ ] **Step 4: 在 5～15 分钟维护窗切换 backend**

停止旧 backend，保留 Neuron、NanoMQ、TimescaleDB。用 release manifest 的 ARM64 摘要创建 rc.4 backend，严格复用旧容器的 `network_mode: host`、`tmpfs: /dev/mqueue`、具名卷、env、restart policy 和端口行为。由 owner migration job 应用 045，application role 只在 Schema=045 后启动。

切换后检查：

```sql
SELECT max(version) FROM schema_migrations;
SELECT count(*), min(created_at), max(created_at), pg_size_pretty(pg_total_relation_size('t_l0_observation_dedup')) FROM t_l0_observation_dedup;
SELECT application_name, proc_name, schedule_interval, scheduled FROM timescaledb_information.jobs WHERE hypertable_name IN ('t_telemetry','tel_agg_1h','tel_agg_1d') OR proc_name='prune_l0_observation_dedup' ORDER BY proc_name;
SELECT count(*) FROM tel_agg_1h;
SELECT count(*) FROM tel_agg_1d;
```

Expected: Schema `045`；防重缓存约 6 小时且低于 500MB；每类 job 恰好一份；1h/1d 有数据。

- [ ] **Step 5: 做容量、健康与 10 分钟实时验收**

验收门槛全部满足：

- 根盘使用率 `<75%`；数据盘可用空间 `>=6GB`；
- backend `running/healthy`、restart 0、运行错误 0；
- 公网 liveness 显示 `0.4.85-rc.4`；
- 认证 L2 realtime 为 HTTP 200、`fresh=true`、`quality_good=true`；
- 连续 10 分钟每 30 秒采样，L0/L2 时间戳一致且延迟始终 `<30s`；
- 不发送控制，不运行自动策略。

任一关键门槛失败：停止 rc.4，恢复 `pre-v0.4.85-rc.4-schema044` dump，并用 rc.3 固定摘要启动；不得做部分表手工回填。

- [ ] **Step 6: 固化部署证据并提交**

`docs/deploy-1号机-v0.4.85-rc.4-http.md` 必须写入：源码 commit、Actions run、ARM64 digest、TimescaleDB 版本、备份路径/SHA、逐项清理路径、迁移前后空间、Schema/jobs、10 分钟 21 个样本摘要和回滚身份。同步 `CODEX_HANDOFF.md` 后：

```powershell
git add 'docs/deploy-1号机-v0.4.85-rc.4-http.md' CODEX_HANDOFF.md
git commit -m "docs(deploy): record rc.4 storage acceptance"
git push origin ticket/v0.4.85-node-data-trunk-hard-cut
git status --short
```

Expected: 工作树为空，远端包含最终部署证据。

---

## Final Verification Matrix

| 规格要求 | 证明任务 |
|---|---|
| 6 小时防重复缓存、15 分钟清理 | Task 2 Steps 4–5 |
| 历史证据不依赖缓存 | Task 2 Steps 2–3 |
| 超期重放不重复入库 | Task 1 + Task 2 Step 9 |
| 1h chunk、6h 压缩、7d 明细 | Task 2 Steps 6–7 |
| 1h/1d 长期聚合，5min 不启用 | Task 2 Steps 6–7 |
| fresh/upgrade/replay/损坏拒绝 | Task 2 Step 8 |
| 固定 rc.4/045 发布身份 | Task 3 |
| 精确清理、可恢复备份、固定摘要 | Task 4 Steps 1–4 |
| 容量、健康、实时验收与部署证据 | Task 4 Steps 5–6 |
