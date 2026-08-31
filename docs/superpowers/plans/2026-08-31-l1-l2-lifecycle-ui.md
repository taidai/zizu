# L1/L2 生命周期界面闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不恢复独立 L1 页面和旧实体 CRUD 的前提下，让实施工程师在“标准实体”页完成已有加工查看/修改、共享模板维护、模板安装/升级和安全停用。

**Architecture:** 继续使用现有不可变点位加工修订与 plan/apply 单一发布缝。停用也先生成 `delete_candidate` 计划，再由同一个 apply 事务把当前安装和 L2 实体退出运行态；历史、来源和审计不删除。界面把这些能力折叠进“数据来源与计算”，普通实时/历史视图保持简洁。

**Tech Stack:** FastAPI、Python 3.12、PostgreSQL/TimescaleDB、React 18、TypeScript、Node test runner、Playwright

**Spec:** `docs/superpowers/specs/2026-08-29-inline-l0-point-processing-design.md`

## Global Constraints

- 唯一主干为“真实节点树 → L0 原始点位 → L1 点位加工 → L2 全局实体”。
- 普通节点页只保留“原始点位”和“标准实体”；不得恢复独立 L1 页面。
- L0 不可被加工配置修改；上层应用只消费 committed L2。
- 配置变更必须预览、摘要复核、配置修订栅栏、幂等 apply 和追加式审计。
- 停用只停止未来计算和上层可见性；不得物理删除 L2 历史、来源证据或不可变修订。
- 不新增依赖、第二套加工引擎、旧 `/entities` CRUD 或运行期 fallback。

---

### Task 1: 安全停用计划与原子应用

**Files:**
- Create: `init-db/migration_056_point_processing_deactivation.sql`
- Modify: `backend/app/services/point_processing.py`
- Modify: `backend/app/services/point_processing_postgres.py`
- Modify: `backend/app/api/point_processings.py`
- Modify: `backend/tests/test_point_processing.py`
- Modify: `backend/tests/test_point_processing_public_api.py`
- Modify: `backend/tests/test_point_processing_postgres.py`
- Modify: `scripts/test_build_release_images.py`

**Interfaces:**
- Produces: `PointProcessingService.preview_deactivation(node_id: UUID, actor: str) -> PointProcessingPlan`
- Produces: `POST /api/v1/nodes/{node_id}/point-processing-deactivation-plan`
- Preserves: `POST /api/v1/point-processing-plans/{plan_id}/apply`

- [ ] **Step 1: Write failing service tests**

Add tests proving an installed node produces one `delete_candidate` per L2 output, a node without an installation is rejected, and an output used by another active processing is blocked.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_point_processing.py -q`

Expected: FAIL because `preview_deactivation` does not exist.

- [ ] **Step 3: Implement the deterministic deactivation plan**

Build a ready/blocked immutable `PointProcessingPlan` from the current revision and stable output identities. Its digest includes node, current revision, output IDs, dependency blockers, actor and base configuration revision.

- [ ] **Step 4: Write failing HTTP and PostgreSQL tests**

Prove the route is configuration-write protected; apply marks the current installation non-current and its output entities inactive; `t_l2_observations`, sources and latest rows remain; idempotent replay returns the same application; applying a later valid processing plan reactivates the same stable entity UUID.

- [ ] **Step 5: Verify RED**

Run: `python -m pytest tests/test_point_processing_public_api.py -q`

Run with isolated PostgreSQL: `$env:ZIZU_POSTGRES_TEST='1'; python -m pytest tests/test_point_processing_postgres.py -q`

Expected: endpoint/transaction assertions fail before implementation.

- [ ] **Step 6: Implement the minimal apply branch and Schema 056**

Schema 056 changes the deferred single-source constraint so inactive entities may intentionally have no current L1 source. The apply branch rechecks the active installation and exact output IDs under the configuration lock, publishes `point_processing.deactivate`, marks entities inactive, marks installation non-current, writes application/idempotency/audit, and leaves time-series evidence untouched. Normal install sets a matching inactive entity back to `active=TRUE`.

- [ ] **Step 7: Verify GREEN**

Run the three focused suites above and `python -m unittest scripts.test_build_release_images -v`.

- [ ] **Step 8: Commit**

Commit message: `feat: add safe point processing deactivation`

### Task 2: 现有加工、模板与安装升级回到标准实体页

**Files:**
- Create: `frontend/src/components/data-trunk/pointProcessingLifecycleModel.ts`
- Create: `frontend/src/components/data-trunk/pointProcessingLifecycleModel.test.mjs`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/pages/NodeTreePage.tsx`
- Modify: `frontend/src/components/data-trunk/DataTrunkWorkspace.tsx`
- Modify: `frontend/src/components/data-trunk/PointProcessingTemplateManager.tsx`
- Modify: `frontend/src/components/data-trunk/PointProcessingPlanPanel.tsx`
- Modify: `frontend/src/components/data-trunk/EntityDataPanel.tsx`

**Interfaces:**
- Consumes: existing template list/export/validate/import, node-private draft plan, shared plan and common apply.
- Produces: `createPointProcessingDeactivationPlan(nodeId: string) -> Promise<PointProcessingPlan>`.
- Produces: one collapsed “数据来源与计算” workspace inside the standard-entity page.

- [ ] **Step 1: Write failing lifecycle model tests**

Cover current-revision selection, preservation of current input bindings while editing, install/upgrade recommendation, deactivation summary, and operator read-only state.

- [ ] **Step 2: Verify RED**

Run: `node --test src/components/data-trunk/pointProcessingLifecycleModel.test.mjs`

Expected: FAIL because the lifecycle model is missing.

- [ ] **Step 3: Implement the lifecycle model**

Keep state derivation pure: current revision wins, then exact recommended shared revision; current bindings are copied only when compatible; deactivation view says “停止生成新数据，保留历史和来源”。

- [ ] **Step 4: Add the integrated workspace**

In “标准实体”, show current L1 revision/output count and a collapsed “数据来源与计算”. Engineers can load the current immutable content, revise it through a node-private draft plan, inspect/apply it, select a shared template and install/upgrade it. Admins additionally see the existing shared-template library editor. Operators can only inspect current source evidence. No third node tab is added.

- [ ] **Step 5: Add safe deactivation UI**

The destructive-looking action is named “停用加工”; first generate and display the reviewed plan, then require a second click to apply. Success refreshes the entity catalog; no UI path issues a hard delete.

- [ ] **Step 6: Verify GREEN**

Run: `node --test src/**/*.test.mjs`

Run: `npm run build`

- [ ] **Step 7: Commit**

Commit message: `feat: restore l1 lifecycle in entity workspace`

### Task 3: 无头主干生命周期验收与交接

**Files:**
- Modify: `frontend/e2e/node-management.spec.ts`
- Modify: `docs/acceptance-checklist.md`
- Modify: `CODEX_HANDOFF.md`

**Interfaces:**
- Produces: one serial E2E journey covering create → inspect → revise/new revision → select/install/upgrade → deactivate.

- [ ] **Step 1: Extend the E2E journey before relying on it**

Use only the isolated `E2E验证` tree. Assert visible template catalog, revision publication, installation/upgrade plan, deactivation review/apply, disappearance from active L2, and retained historical/source evidence. Do not enable rules or send device control.

- [ ] **Step 2: Run local static E2E validation**

Run: `npm run test:e2e:node:list`

Run: `npx playwright test e2e/node-management.spec.ts --grep "L1 lifecycle"` only when a disposable test endpoint and secret environment are explicitly available.

- [ ] **Step 3: Run final gates**

Run: `python -m pytest tests -q -p no:cacheprovider`

Run: `python -m unittest discover -s scripts -p "test_*.py"`

Run: `node --test src/**/*.test.mjs` and `npm run build` in `frontend`.

- [ ] **Step 4: Update handoff and commit**

Record exact test counts, skipped live checks, schema/version impact and the next deployment step. Commit message: `test: cover point processing lifecycle journey`.
