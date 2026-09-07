# ZiZu Demo Final Product Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:dispatching-parallel-agents to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the formal ZiZu v1.0.5 application match the confirmed local tablet Demo's page structure and interactions while retaining only real production data, permissions, JDM and control semantics.

**Architecture:** Keep the v1.0.4 backend and formal React application as the only runtime. First freeze a visual/interaction contract and common shell, then run two isolated three-lane waves, integrate each wave serially, and finish with full formal API, viewport, safety and deployment verification. Add only one backend seam: auditable fixed workbench-slot bindings with deterministic exact-key fallback.

**Tech Stack:** React 18, TypeScript, Vite, Tailwind/CSS, Playwright, FastAPI, PostgreSQL/TimescaleDB, GoRules JDM, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-08-demo-final-product-parity-design.md`

## Global Constraints

- Base every lane on `origin/main` commit `2f36774`; do not develop from the dirty historical root checkout.
- Demo controls page structure and interaction only; never import its synthetic data, scenario switch, role switch, reset, fault injection, simulated notification or simulated readback.
- Formal data is committed L2 only. Missing, stale, bad, disconnected, forbidden and conflicting states must fail closed and remain visible.
- Existing authentication, permissions, configuration revision, JDM, idempotent command, device-write and readback contracts remain authoritative.
- No new dependency, microservice, cache, rule engine, arbitrary scripting or page designer.
- Do not let parallel lanes edit `frontend/src/App.tsx`, `frontend/src/index.css`, `VERSION`, package versions, README files or deployment files.
- Use test-first changes for every new behavior; a production change is not complete until its targeted tests and build pass.
- Do not write a real device, enable a site strategy, publish live processing or send a test notification during local or first-pass field acceptance.

---

### Task 1: Freeze the Demo parity contract and common shell

**Files:**
- Create: `docs/reviews/2026-09-08-demo-parity-matrix.md`
- Create: `.release-artifacts/v1.0.5/demo-golden/*.png` (ignored evidence, 7 pages × 2 viewports)
- Modify: `frontend/src/appNavigationModel.ts`
- Modify: `frontend/src/appNavigationModel.test.mjs`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/index.css`
- Test: `frontend/e2e/tablet-shell.spec.ts`

**Interfaces:**
- Produces: `TabletPage`/`pagesForArea` mapping in which runtime exposes `workbench`, `monitor`, `controls`, and engineering exposes `tree`, `alarms`, `strategies`, `admin` without granting new permissions.
- Produces: shared `.zizu-*` shell and `--zizu-red/#bb0814`, `--zizu-gold/#b88a34`, bright-silver tokens; page lanes consume these and add only namespaced local CSS.

- [ ] **Step 1: Capture the reference before production edits**

Run the existing Demo at `http://127.0.0.1:19100` and capture `overview`, `devices`, `engineering`, `alarms`, `automation`, `control`, `tools` at 1280×800 and 1024×768. Record for every page: visible sections, primary buttons, tables, dialogs, navigation destination, empty/error behavior, and Demo-only controls that must be absent from production.

- [ ] **Step 2: Write failing navigation and shell tests**

Extend `appNavigationModel.test.mjs` and `tablet-shell.spec.ts` so they require: runtime bottom navigation with three destinations; engineering navigation with four destinations; operator cannot enter engineering; alarm event navigation remains reachable from the overview; no text `前端 DEMO`, `切换场景`, `切换角色`, `重置演示`, `模拟回读` in the production build; main controls and focus targets are at least 44px at both viewports.

- [ ] **Step 3: Verify RED**

Run:

```powershell
node --test --experimental-strip-types src/appNavigationModel.test.mjs
npx playwright test e2e/tablet-shell.spec.ts
```

Expected: the new three-runtime/four-engineering navigation and Demo-control absence assertions fail against v1.0.4.

- [ ] **Step 4: Implement the minimal shell**

Change only the formal `App.tsx`, navigation model and shared tokens. Retain `LoginGate`, session restore, server role resolution, health bar, lazy loading and logout. Do not copy Demo hash routing or global `.panel/.button/.modal` selectors.

- [ ] **Step 5: Verify GREEN and commit**

Run the two targeted commands plus `npm run build`; commit `feat(ui): freeze final tablet shell`.

### Task 2: Runtime overview, fixed slots and manual control

**Files:**
- Create: `init-db/migration_063_ems_workbench_slots.sql`
- Create: `backend/app/services/ems_workbench_slots.py`
- Create: `backend/app/services/ems_workbench_slots_postgres.py`
- Modify: `backend/app/services/ems_workbench.py`
- Modify: `backend/app/api/ems_workbench.py`
- Modify: `backend/app/main.py` only if dependency wiring requires it
- Test: `backend/tests/test_ems_workbench.py`
- Create/Test: `backend/tests/test_ems_workbench_slots_postgres.py`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/pages/EMSWorkbenchPage.tsx`
- Create: `frontend/src/components/runtime-monitoring/workbench.css`
- Create/Test: `frontend/src/components/runtime-monitoring/workbenchSlotsModel.test.mjs`
- Create: `frontend/src/components/runtime-monitoring/workbenchSlotsModel.ts`
- Create/Test: `frontend/e2e/tablet-control.spec.ts`
- Modify/Test: `frontend/e2e/tablet-runtime.spec.ts`

**Interfaces:**
- Produces: fixed slot keys `site-power`, `pv-power`, `storage-power`, `storage-soc`, `charging-power`.
- Produces: a workbench response for each slot containing `binding_mode: manual|exact|unconfigured|ambiguous`, one optional `entity`, and a reason.
- Produces: engineer/admin write seam accepting `{entity_instance_id: string|null, base_configuration_revision: number}` with idempotency key; it publishes one configuration revision transactionally and operator writes return 403.
- Consumes: committed L2 descriptors/readings, existing control-confirmation→command→reconcile APIs and Task 1 shell tokens.

- [ ] **Step 1: Write failing domain tests for deterministic slots**

Add tests proving: one exact canonical definition binds automatically; zero candidates is unconfigured; two candidates is ambiguous; a valid manual binding wins; deleted/inactive/wrong-type/wrong-unit manual targets fail closed; power requires numeric kW and SOC requires numeric `%`; exact aliases are an explicit constant list and never use substring/display-name matching.

- [ ] **Step 2: Verify RED**

Run `python -m unittest tests.test_ems_workbench -v`; expected failure is missing slot binding behavior, not test setup.

- [ ] **Step 3: Implement slot resolution and PostgreSQL persistence**

Create migration 063 with one current row per fixed slot, a foreign key to entity instance, audit timestamps and no history duplication outside existing configuration audit. Save/clear in one database transaction using `PostgresConfigurationRevisions.publish`; reject stale base revision and replay the same idempotency key without allocating another revision.

- [ ] **Step 4: Add API tests and implementation**

Test engineer/admin save, operator 403, stale revision 409/422 stable code, nonexistent entity, type/unit mismatch, clear-to-auto and idempotent replay. Implement the smallest REST surface under `/api/v1/ems-workbench/slots/{slot_key}` and include resolved slots in `GET /ems-workbench`.

- [ ] **Step 5: Write failing frontend model and Browser tests**

Require the Demo overview composition, five real slots, neutral topology for unconfigured/ambiguous direction, a binding dialog only for configuration roles, no fake zero, metric detail/history/provenance, and controls using only the formal directory. In control tests assert confirmation ID and idempotency headers, waiting-readback state, no client-side L2 mutation, expiry/error behavior, and no extra write on repeated clicks.

- [ ] **Step 6: Implement the formal overview and control layout**

Recompose `EMSWorkbenchPage` with Demo sections, use slot model/API values, reuse `EntityRuntimeDetail`, and retain the existing control methods. All advanced frame/config/source evidence belongs in the detail drawer/dialog.

- [ ] **Step 7: Verify and commit**

Run focused backend tests, all runtime model tests, the two runtime/control Playwright specs and `npm run build`; commit `feat(workbench): deliver real demo overview and control`.

### Task 3: Device monitor parity

**Files:**
- Modify: `frontend/src/pages/DeviceMonitorPage.tsx`
- Modify: `frontend/src/components/runtime-monitoring/runtimeModel.ts`
- Modify: `frontend/src/components/runtime-monitoring/runtimeModel.test.mjs`
- Modify: `frontend/src/components/runtime-monitoring/runtime-monitoring.css`
- Modify/Test: `frontend/e2e/tablet-devices.spec.ts`

**Interfaces:**
- Consumes: Task 1 shell tokens and existing `/nodes`, `/entity-instances`, committed snapshot/WS, `/alarms/counts`, entity history and data-trunk APIs.
- Produces: six-card pagination, real category/search/alarm filters, quality-aware summary counts and one detail dialog with 10/20 entity pagination, history and provenance.

- [ ] **Step 1: Write failing model tests**

Add assertions for quality summary (`current`, `last`, `unconfigured`, `unknown`), deterministic device category from explicit node type only, stable main/secondary metric ordering from exact definition IDs, six-card pages, clearing filters, and failed alarm counts disabling alarm-only filtering.

- [ ] **Step 2: Verify RED**

Run `node --test --experimental-strip-types src/components/runtime-monitoring/runtimeModel.test.mjs`; expected failures are the new summary/category/ordering exports.

- [ ] **Step 3: Implement the minimal model and page composition**

Use no hard-coded node IDs or values. Unknown categories render as “其他”; unknown metrics remain visible instead of being guessed. Reuse the existing entity detail and provenance models.

- [ ] **Step 4: Extend Browser acceptance**

At both target viewports assert two rows of three cards where space permits, six-card pagination, 10-row entity detail, 20-row option, search/category/alarm filtering, GOOD versus last-value styling, history/source requests and configuration jump visibility by role.

- [ ] **Step 5: Verify and commit**

Run the focused model test, `npx playwright test e2e/tablet-devices.spec.ts`, and `npm run build`; commit `feat(devices): match final monitor experience`.

### Task 4: Node, L0, L1 and L2 engineering core

**Files:**
- Modify: `frontend/src/pages/NodeTreePage.tsx`
- Modify: `frontend/src/components/NodeTagPanel.tsx`
- Modify: `frontend/src/components/data-trunk/DataTrunkWorkspace.tsx`
- Modify: `frontend/src/components/data-trunk/InlinePointProcessingPanel.tsx`
- Modify: `frontend/src/components/data-trunk/EntityDataPanel.tsx`
- Modify: `frontend/src/components/data-trunk/EntityObservationCard.tsx`
- Modify: `frontend/src/components/node/tabletEngineering.css`
- Modify/Test: relevant `frontend/src/components/data-trunk/*.test.mjs`
- Modify/Test: `frontend/e2e/node-management.spec.ts`
- Modify/Test: `frontend/e2e/tablet-processing-recovery.spec.ts`

**Interfaces:**
- Consumes: Task 1 shell tokens and existing node/tag/import/data-trunk/processing-plan/apply/history APIs.
- Produces: Demo-density node tree and L0/L1/L2 workspaces without changing raw values, plan/apply semantics or cross-node L2-only rule.

- [ ] **Step 1: Write failing UI/model tests**

Require three explicit user views—原始数据、点位加工、标准实体—while keeping L0/L1/L2 as data views rather than tree nodes. Require 10/20 pagination, refresh/link diagnosis, selected-L0 processing dialog, cross-node source labels restricted to L2, check-before-publish, revision-conflict recovery and advanced evidence disclosure.

- [ ] **Step 2: Verify RED**

Run all data-trunk model tests and list the two Playwright specs; the new labels/actions/layout assertions must fail for the expected missing presentation behavior.

- [ ] **Step 3: Recompose without changing the domain contracts**

Move existing actions into the confirmed Demo tables/dialogs. Preserve node CRUD, Neuron result-unknown reconciliation, raw value fidelity, strong types, DAG validation, optional templates, configuration digest and idempotent apply.

- [ ] **Step 4: Verify and commit**

Run all data-trunk/node model tests, the two Playwright specs against formal APIs, and `npm run build`; commit `feat(engineering): unify node l0 l1 l2 workflow`.

### Task 5: Integrate and accept Wave 1

**Files:**
- Modify only to resolve integration: `frontend/src/App.tsx`, `frontend/src/index.css`, lane-owned files where a real conflict exists
- Update: `docs/reviews/2026-09-08-demo-parity-matrix.md`

**Interfaces:**
- Consumes: reviewed commits from Tasks 2, 3 and 4 based on the same Task 1 commit.
- Produces: one integration branch with runtime overview/control, devices and engineering core.

- [ ] **Step 1: Cherry-pick reviewed lane commits in order 2, 3, 4**

Resolve only actual textual conflicts; never choose a lane's global CSS or navigation wholesale over Task 1.

- [ ] **Step 2: Run Wave 1 focused and full frontend gates**

Run all `frontend/src/**/*.test.mjs`, `npm run build`, `tablet-shell`, `tablet-runtime`, `tablet-control`, `tablet-devices`, `node-management`, and `tablet-processing-recovery`.

- [ ] **Step 3: Capture formal screenshots**

Capture overview, devices, control and engineering at both viewports; compare structure and interaction against the golden matrix. Differences caused only by real values are allowed; missing sections, unreachable actions, overlap and hidden controls are not.

- [ ] **Step 4: Commit integration fixes**

Commit `fix(ui): integrate first demo parity wave` only if integration changes were required.

### Task 6: Alarm center parity

**Files:**
- Modify: `frontend/src/pages/AlarmCenterPage.tsx`
- Modify: `frontend/src/pages/AlarmConfigurationPage.tsx`
- Modify: `frontend/src/components/alarm-configuration/RuleSetEditor.tsx`
- Modify: `frontend/src/components/alarm-center/AlarmNotificationRecords.tsx`
- Modify: `frontend/src/components/alarm-center/tabletApplications.css`
- Modify/Test: `frontend/src/components/alarm-center/*.test.mjs`
- Modify/Test: `frontend/src/components/alarm-configuration/*.test.mjs`
- Modify/Test: `frontend/e2e/tablet-applications.spec.ts`
- Modify/Test: existing alarm E2E specs

**Interfaces:**
- Consumes: real alarm events, transitions, acknowledgements, archives, rule-set trial/plan/apply and HTTP delivery APIs.
- Produces: current/history/notification 10-row tables and modal/drawer actions; no simulated fault endpoint.

- [ ] **Step 1: Add failing tests for page density and modal actions**

Require current/history/notification tabs, 10 default rows/20 option, current-page selection, confirm/detail/archive/delete eligibility, rule editor dialog and HTTP delivery detail with explicit loading/error/permission states.

- [ ] **Step 2: Verify RED, implement, verify GREEN**

Run alarm model tests, implement only layout/action wiring, then run alarm model tests and alarm Playwright suites.

- [ ] **Step 3: Build and commit**

Run `npm run build`; commit `feat(alarms): match final table and dialog flow`.

### Task 7: Generic JDM dispatch strategy parity

**Files:**
- Modify: `frontend/src/pages/DispatchStrategyPage.tsx`
- Modify: `frontend/src/components/dispatch-strategy/NativeDecisionTableEditor.tsx`
- Modify: `frontend/src/components/dispatch-strategy/nativeDecisionTableModel.ts`
- Modify/Test: `frontend/src/components/dispatch-strategy/*.test.mjs`
- Modify/Test: `frontend/e2e/dispatch-strategy.spec.ts`

**Interfaces:**
- Consumes: existing strategy list/draft/simulate/publish/enable/disable/failure-latch/events APIs and native JDM editor.
- Produces: three-step select-inputs→edit-one-native-table→bind-controls flow; full graph remains lossless fallback.

- [ ] **Step 1: Add failing tests for the three-step lifecycle**

Require multiple typed L2 inputs, editable native table columns/rows, multiple controllable outputs, optional example columns, dirty-state invalidation, draft receipt, no-write simulate, explicit publish then enable, failure lock and event/readback evidence.

- [ ] **Step 2: Verify RED and implement the minimal presentation adapter**

Do not create a second schedule data model and do not rewrite unknown JDM graph fields. Native editor remains lazy loaded.

- [ ] **Step 3: Verify and commit**

Run dispatch model tests, `npx playwright test e2e/dispatch-strategy.spec.ts`, and `npm run build`; commit `feat(strategies): deliver generic native jdm workflow`.

### Task 8: System tools parity

**Files:**
- Modify: `frontend/src/components/AdminPanel.tsx`
- Modify: `frontend/src/components/NanoMQManager.tsx`
- Modify: `frontend/src/components/FaultMapManager.tsx`
- Modify: `frontend/src/components/admin/AlarmHttpNotificationPanel.tsx`
- Create/Modify: namespaced local tools CSS and tests next to these components
- Modify/Test: relevant admin E2E specs

**Interfaces:**
- Consumes: existing pipeline/MQTT, NanoMQ, HTTP notification, fault map, data browser and system state APIs.
- Produces: Demo-style grouped tools landing page and unchanged real CRUD/test flows with server permission enforcement.

- [ ] **Step 1: Add failing grouping, permission and action tests**

Require four clear groups, modal/drawer editors, disabled/hidden writes for insufficient roles, visible danger confirmation for destructive data operations, HTTP test result receipt and no Demo reset action.

- [ ] **Step 2: Verify RED, implement, verify GREEN**

Recompose existing managers; do not add a generic plugin/action framework.

- [ ] **Step 3: Build and commit**

Run focused tests and `npm run build`; commit `feat(tools): organize formal system tools`.

### Task 9: Integrate Wave 2 and run full local acceptance

**Files:**
- Modify for integration only: `frontend/src/App.tsx`, `frontend/src/index.css`, local page CSS
- Update: `docs/reviews/2026-09-08-demo-parity-matrix.md`
- Create: `docs/reviews/2026-09-08-v1.0.5-local-acceptance.md`

**Interfaces:**
- Consumes: reviewed Tasks 6/7/8 and integrated Wave 1.
- Produces: one complete formal v1.0.5 candidate and evidence report.

- [ ] **Step 1: Cherry-pick reviewed Wave 2 commits and resolve only integration conflicts**

- [ ] **Step 2: Run all unit/build gates**

Run backend full unittest discovery, script tests, all frontend model tests and `npm run build`. Record pass/fail/skip counts verbatim.

- [ ] **Step 3: Run formal Browser acceptance**

At 1280×800 and 1024×768 cover all seven pages, every visible primary action, tables/dialogs, permission roles, loading/empty/disconnected/stale/timeout/conflict/unknown-result states, and the fixed mainline `节点 → L0 → L1 → L2 → 告警 → 调度策略 → 控制 → 工作台`. Use isolated data and protocol boundaries; perform no 1号机 write.

- [ ] **Step 4: Run a production-bundle prohibited-content scan**

Fail if production output contains Demo-only UI text or imports from `demo-tablet`; record that synthetic fixed values and simulated readback code are absent.

- [ ] **Step 5: Capture final screenshots and close every matrix row**

Any failed, skipped without an accepted environmental reason, overlapping or unreachable row keeps the result `INCOMPLETE`.

### Task 10: Version, documentation, immutable release and one deployment

**Files:**
- Modify: `VERSION`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `CODEX_HANDOFF.md`
- Create: `docs/deploy-1号机-v1.0.5-http.md`

**Interfaces:**
- Consumes: Task 9 complete local candidate.
- Produces: v1.0.5 source commit/tag, fixed ARM64 digest, verified rollback anchor, one 1号机 ZiZu container replacement and read-only field result.

- [ ] **Step 1: Write failing version/release tests and bump every public version to 1.0.5**

- [ ] **Step 2: Run complete release gates and independent whole-branch review**

- [ ] **Step 3: Build/pull the immutable ARM64 image and verify its digest**

- [ ] **Step 4: Before touching 1号机, create and validate the scoped backup/restore evidence required by the current deployment checklist**

- [ ] **Step 5: Replace only the ZiZu application container using the established host network and `/dev/mqueue`; do not restart TimescaleDB, NanoMQ or Neuron**

- [ ] **Step 6: Run headless plus visible Browser read-only acceptance along the full mainline**

Record actual version, digest, Schema, configuration revision, container health/restarts, service continuity, screenshots, console/network errors and every skipped write action. A successful deployment without a complete read-only mainline remains `INCOMPLETE`.

- [ ] **Step 7: Push the reviewed branch/tag and update handoff**

Do not claim full EMS field delivery unless the report contains the separately authorized real control, alarm delivery and live data evidence.
