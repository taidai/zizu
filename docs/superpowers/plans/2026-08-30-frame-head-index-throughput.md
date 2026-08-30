# Frame Head Index Throughput Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 1 号机在持续 1 秒输入下直接定位最老未完成帧，消除每拍扫描全部历史帧造成的处理积压和超龄失败。

**Architecture:** 保留单写者、事务 A、L1 DAG、事务 B、同步提交和统一 outbox 的既有工业语义，只用 Schema 053 将 `t_data_frames` 的未完成帧部分索引改为以 `frame_sequence` 排序。迁移可重放；运行查询和上层消费接口不变。

**Tech Stack:** PostgreSQL/TimescaleDB SQL migration、Python unittest/psycopg2、FastAPI、Docker Buildx、ARM64 Docker Compose。

**Spec:** `docs/superpowers/specs/2026-08-27-zizu-platform-core-architecture-design.md`

## Global Constraints

- 唯一主干仍是“真实节点树 → L0 原始点位 → L1 点位加工 → L2 全局实体 → 告警/JDM/控制/固定 EMS 工作台”。
- 数据帧继续使用单写者、统一 1 秒节拍、事务 A/B、提交后可见和 60 秒处理预算。
- 不关闭 `fsync`、`synchronous_commit` 或 `full_page_writes`，不增加 Redis/Kafka/微服务或第二规则引擎。
- 只修改 ZiZu 项目文件；保留工作区中与本任务无关的已有改动和未跟踪文件。
- 发布版本为 `0.4.96`，Schema 为 `053`，1 号机继续使用固定 ARM64 摘要、`network_mode: host` 和 `/dev/mqueue` tmpfs。

---

### Task 1: 用真实 PostgreSQL 行为锁定帧头索引

**Files:**
- Modify: `backend/tests/test_data_frames_migration_postgres.py`
- Create: `init-db/migration_053_frame_head_index.sql`

**Interfaces:**
- Consumes: Schema 046 的 `t_data_frames(frame_sequence,status)` 与 `ix_data_frames_claim`。
- Produces: 可重放的 Schema 053；查询 `WHERE status IN ('PENDING','PROCESSING') ORDER BY frame_sequence LIMIT 1` 使用 `ix_data_frames_claim`，不再过滤全部终态历史帧。

- [ ] **Step 1: 写失败的 PostgreSQL 行为测试**

在迁移测试中先应用 046，插入 2,000 个终态历史帧和 1 个待处理帧，应用 053，再通过 `EXPLAIN (ANALYZE, FORMAT JSON)` 断言真实计划使用 `ix_data_frames_claim`，且索引节点的 `Rows Removed by Filter` 为 0。这个测试能捕获“索引仍按 status/lease 排序、查询退回 frame_sequence 全表过滤”的生产回归。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m unittest backend.tests.test_data_frames_migration_postgres.DataFramesMigrationPostgresTest.test_053_claims_oldest_unfinished_frame_without_scanning_terminal_history -v`

Expected: FAIL，因为 `migration_053_frame_head_index.sql` 尚不存在或旧索引仍扫描终态帧。

- [ ] **Step 3: 写最小可重放迁移**

迁移必须验证 `t_data_frames` 与旧索引存在，在单事务中删除旧 `ix_data_frames_claim`，重建为：

```sql
CREATE INDEX ix_data_frames_claim
  ON public.t_data_frames(frame_sequence)
  WHERE status IN ('PENDING','PROCESSING');
```

再次执行时必须核验索引定义正确；遇到未知部分结构必须以 `SCHEMA_053_PARTIAL_STRUCTURE` 拒绝，不静默修补。

- [ ] **Step 4: 运行专项 PostgreSQL 测试并确认 GREEN**

Run: `python -m unittest backend.tests.test_data_frames_migration_postgres -v`

Expected: 全部 PASS；053 重放后仍使用新的部分顺序索引。

- [ ] **Step 5: 提交迁移**

```bash
git add backend/tests/test_data_frames_migration_postgres.py init-db/migration_053_frame_head_index.sql docs/superpowers/plans/2026-08-30-frame-head-index-throughput.md
git commit -m "perf(frames): index unfinished frame head"
```

### Task 2: 发布 0.4.96 / Schema 053

**Files:**
- Modify: `VERSION`
- Modify: `backend/app/VERSION`
- Modify: `backend/pyproject.toml`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `scripts/test_build_release_images.py`

**Interfaces:**
- Consumes: Task 1 的 Schema 053。
- Produces: 版本一致的 `0.4.96` 源码、前后端包元数据和发布脚本测试期望。

- [ ] **Step 1: 先把发布脚本期望改为 Schema 053 并确认 RED**

Run: `python -m unittest scripts.test_build_release_images -v`

Expected: FAIL，因为源版本仍为 0.4.95 或 latest migration 尚未是 053。

- [ ] **Step 2: 最小版本进位**

仅把六个版本来源从 `0.4.95` 改为 `0.4.96`，把构建测试的 Schema 期望从 `052` 改为 `053`；不改依赖版本。

- [ ] **Step 3: 运行自动门禁**

Run: 后端完整 unittest、显式 PostgreSQL 专项、scripts unittest、前端 test、`npm run build`、`compileall`、`git diff --check`。

Expected: 0 failure，只有明确要求外部环境的项目允许 skip。

- [ ] **Step 4: 提交并创建发布标签**

```bash
git add VERSION backend/app/VERSION backend/pyproject.toml frontend/package.json frontend/package-lock.json scripts/test_build_release_images.py
git commit -m "chore: release 0.4.96"
git tag v0.4.96
git push origin main
git push origin v0.4.96
```

### Task 3: 固定摘要部署与主干验收

**Files:**
- Create: `docs/deploy-1号机-v0.4.96-http.md`
- Modify: `CODEX_HANDOFF.md`

**Interfaces:**
- Consumes: GitHub Actions 产出的 ARM64 固定摘要和 Schema 053。
- Produces: 1 号机可复核的镜像身份、数据库计划、持续吞吐和 Browser 主干证据。

- [ ] **Step 1: 部署前备份并校验**

对 `omnithings` 做 custom-format `pg_dump`，记录大小、SHA-256，并用 `pg_restore -l` 验证目录可读；不清空现场数据。

- [ ] **Step 2: 只按固定 ARM64 摘要重建 backend**

保留旧容器的环境、业务卷、host network、`/dev/mqueue` tmpfs、安全限制和重启策略；确认 Schema 053 已执行、backend healthy、restart=0、`/health` 为 `0.4.96`。

- [ ] **Step 3: 验证根因关闭**

生产库执行同一条 `EXPLAIN (ANALYZE, BUFFERS)`；必须使用 `ix_data_frames_claim`，不得再出现扫描约 72,000 条终态帧。持续观测至少 5 分钟：frame head 前进，PENDING/PROCESSING 不增长，最老未完成帧龄不增长，outbox 归零，窗口内无新的 `FRAME_PROCESSING_FAILED` 超龄帧。

- [ ] **Step 4: 用已登录 Browser 走完整主干**

只读访问节点树、PCS L0 实时/历史、L1 检查结果、L2 实时/历史/来源、告警中心和 JDM/EMS 工作台；不得发布配置、启停规则、确认告警或写设备。记录版本、数据质量、时间戳和来源是否一致。

- [ ] **Step 5: 写交付记录并提交**

在部署记录中如实写出 PASS/INCOMPLETE/FAIL 证据，更新 `CODEX_HANDOFF.md`，提交并推送；任何门禁或 Browser 主干未通过时不得宣称交付完成。
