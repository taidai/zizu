# PCS L0 Recovery And L1 Template Maintenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 恢复 PCS committed L0 数据显示，并交付管理员可视化维护共享点位加工模板的最小闭环。

**Architecture:** 保持现有单站黑板、不可变帧、单 JSON 模板和 plan/apply 主链。L0 只修复过期 claim 与迁移前快照读取；L1 以纯前端草稿模型生成既有模板 JSON，后端只增加零副作用校验并收紧权限。

**Tech Stack:** Python 3.12、FastAPI、PostgreSQL/TimescaleDB、React 18、TypeScript、Node test、现有 Tailwind。

**Spec:** `docs/superpowers/specs/2026-08-28-pcs-l0-recovery-and-l1-template-maintenance-design.md`

## Global Constraints

- 不新增依赖、表、微服务、自由脚本或第二套模板模型。
- 新 committed frame 严格类型读取；兼容只限迁移前 `frame_sequence=0`。
- 模板修订不可变；管理员维护共享模板，工程师安装模板。
- 每个生产改动必须先看到对应测试按预期失败。

---

### Task 1: 恢复过期数据帧

**Files:**
- Modify: `backend/tests/test_data_frames_postgres.py`
- Modify: `backend/app/services/data_trunk_postgres.py`

**Interfaces:**
- Consumes: Migration 048 的 `PROCESSING -> PROCESSING` token 接管约束。
- Produces: `PostgresFrameRepository.claim_next()` 可接管过期租约且不消耗一次业务处理尝试。

- [ ] **Step 1: Write the failing test**

把过期租约回归断言改为重新 claim 后 `attempt_count == 1`，并使用当前 Schema 049 全量迁移创建测试库。

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIZU_POSTGRES_TEST=1 backend/.venv/Scripts/python.exe -m unittest tests.test_data_frames_postgres.DataFramesPostgresTest.test_expired_processing_lease_is_reclaimed_and_old_token_is_fenced`
Expected: FAIL with `DATA_FRAME_CLAIM_FAILED` or attempt count mismatch.

- [ ] **Step 3: Write minimal implementation**

在 `claim_next()` 中区分 PENDING 与 PROCESSING：PENDING 首次 claim 才 `attempt_count + 1`；过期 PROCESSING token 接管保持原计数。60 秒/3 次预算终结保持不变。

- [ ] **Step 4: Run test to verify it passes**

Run the same test; Expected: PASS.

### Task 2: 迁移前 L0 最后值可见且超时

**Files:**
- Modify: `backend/tests/test_committed_frame_stream_postgres.py`
- Modify: `backend/app/services/committed_frame_stream_postgres.py`
- Modify: `frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs`
- Modify: `frontend/src/components/NodeTagPanel.tsx`
- Modify: `frontend/src/components/data-trunk/dataTrunkViewModel.ts`

**Interfaces:**
- Produces: 旧快照的非空值列作为诊断值返回，`effective_quality=STALE`；新帧仍严格按 data_type。

- [ ] **Step 1: Write the failing backend test**

插入 `frame_sequence=0`、声明 INT 但实际 `raw_value_float=34.1` 的旧 latest，断言 snapshot value 为 `34.1` 且质量为 STALE；再插入新帧同类错列，断言不执行兼容读取。

- [ ] **Step 2: Verify backend RED**

Run the two focused snapshot tests; Expected: legacy value is currently `None` or quality is GOOD.

- [ ] **Step 3: Implement backend compatibility boundary**

增加只接受 `frame_sequence in (None, 0)` 的 `_legacy_l0_value()`；旧值按实际唯一非空列读取并强制 STALE，其他行继续 `_typed_value()`。

- [ ] **Step 4: Write and verify frontend RED/GREEN**

增加 `projectRawPointValue(value, quality)`，断言 STALE 显示最后值和“超时”，缺失 projection 默认为 STALE 而非 GOOD；NodeTagPanel 使用该模型并为质量加可见状态色。

### Task 3: 收紧模板权限并增加零副作用检查

**Files:**
- Modify: `backend/tests/test_point_processing_public_api.py`
- Modify: `backend/app/api/point_processings.py`

**Interfaces:**
- Produces: `POST /point-processing-templates/validate`；admin-only import/validate；authenticated configuration.read export。

- [ ] **Step 1: Write failing API tests**

断言 anonymous export=401、engineer import/validate=403、admin validate=200 且 registry 未增加模板、admin import=201。

- [ ] **Step 2: Verify RED**

Run: `backend/.venv/Scripts/python.exe -m unittest tests.test_point_processing_public_api`
Expected: current engineer import succeeds, anonymous export succeeds, validate is 404.

- [ ] **Step 3: Implement minimal routes**

import/validate 使用 `SYSTEM_MANAGE`；validate 调用现有 `parse_point_processing_template()` 并返回 public summary、canonical content 与 digest；export 加 `protected(CONFIGURATION_READ)`。

- [ ] **Step 4: Verify GREEN**

Run the same module; Expected: PASS.

### Task 4: 管理员可视化模板维护

**Files:**
- Create: `frontend/src/components/data-trunk/pointProcessingTemplateEditorModel.ts`
- Create: `frontend/src/components/data-trunk/pointProcessingTemplateEditorModel.test.mjs`
- Create: `frontend/src/components/data-trunk/PointProcessingTemplateManager.tsx`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/components/data-trunk/DataTrunkWorkspace.tsx`
- Modify: `frontend/src/pages/NodeTreePage.tsx`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Produces: `cloneTemplateDraft(raw, mode)`、`setTransformKind(...)`、`validatePointProcessingTemplate(raw)`、`importPointProcessingTemplate(raw)` 与管理员维护面板。

- [ ] **Step 1: Write failing model tests**

用字面量模板断言：复制同资产自动 revision+1；另存新模板 revision=1；直通生成 numeric scale=1/offset=0；倍率/偏移保留用户数值；枚举解析 `原值=标准值` 行；公式只保存表达式且不接受空值。

- [ ] **Step 2: Verify RED**

Run the new Node test; Expected: module missing.

- [ ] **Step 3: Implement pure draft model**

只转换既有 canonical JSON，不生成现场 UUID，不使用 eval，不丢弃界面未识别的 sourceContract 或 transform 字段。

- [ ] **Step 4: Build manager UI**

管理员从当前模板进入折叠面板，编辑基本信息、输入表和输出规则；“检查模板”显示错误/摘要；只有检查内容与当前草稿摘要一致时可“发布新版本”。完整 loading、empty、error、success 状态齐全。

- [ ] **Step 5: Thread role explicitly**

App 传 `canManageTemplates={session.user.role === 'admin'}`，NodeTreePage 和 DataTrunkWorkspace 逐层传递；工程师不渲染维护入口，后端仍独立拒绝越权。

- [ ] **Step 6: Verify frontend**

Run all `*.test.mjs`, `npm run build`; Expected: all pass, TypeScript/Vite exit 0.

### Task 5: 现场配置、全量门禁与部署

**Files:**
- Modify: `VERSION`, `frontend/package.json`, `frontend/package-lock.json`, version constants as required by release contract
- Create: `docs/deploy-1号机-v0.4.85-rc.13-http.md`
- Modify: `CODEX_HANDOFF.md`

- [ ] **Step 1: Run complete local verification**

Run backend discovery, `compileall`, frontend all Node tests, production build, release contract and `git diff --check`.

- [ ] **Step 2: Build and publish one ARM64 image**

Bump to rc.13, commit/push, wait for the existing GitHub Actions workflow, resolve the immutable GHCR digest.

- [ ] **Step 3: Back up and deploy 1 号机**

Verify exact running targets, create/verify Schema 049 backup, update only backend image while preserving host network、`/dev/mqueue` tmpfs and restart policy.

- [ ] **Step 4: Reconcile Neuron topic**

Use authenticated Neuron API to update en9_pcs `cmd/data/error1` subscription topics from `/neuron/MQTT` to `neuron/en9_pcs/telemetry`; do not alter device writes or polling addresses.

- [ ] **Step 5: Acceptance**

Confirm frame backlog drains, processor error count stays zero, PCS telemetry timestamps advance, L0 shows values/STALE correctly, admin template validate/import works, engineer import is 403, health/public page pass, and no control/JDM/device write occurs.

