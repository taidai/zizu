# Standard Entity PCS Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让实施工程师从 PCS 节点的一个或多个 L0 原始数据，在同一页完成标准实体的来源/计算定义、同引擎试算、原子发布和结果查看。

**Architecture:** 保留 L0/L1/L2 内部分层，但将 L1 界面收进标准实体的“数据来源与计算”。试算不建第二套引擎：后端把 ready plan 编译成与运行时相同的 `InstalledPointProcessing`，在一个 repeatable-read 事务中读取最新已提交帧，调用同一 `evaluate_processing` 且不持久化结果。

**Tech Stack:** FastAPI / Python 3.12+，PostgreSQL/TimescaleDB，React / TypeScript / Vite，Python `unittest`/`pytest` runner，Node test runner。

**Spec:** `docs/superpowers/specs/2026-08-29-inline-l0-point-processing-design.md`

**Implementation status (2026-08-29):** 本地候选已实现 Tasks 1–4 和 Task 5 的代码门禁；未部署。
新鲜证据为后端 269 passed、真实 PostgreSQL 主干 24 passed、前端 42 passed、TypeScript 与 Vite
生产构建通过。实施中额外用真实 PostgreSQL RED→GREEN 修复 L2 试算的迁移字段漂移，并确保试算只选
COMPLETE 帧、不把 FAILED 帧当成有效快照。现场人工验收与 1 号机发布仍是下一独立步骤。

## Global Constraints

- L0 不可修改；同节点 L1 可读 L0，跨节点只能读 committed L2。
- L1 强类型、版本化、禁任意脚本；发布继续走现有 plan/apply 和配置栅栏。
- 试算必须使用一个已提交不可变帧和生产评估器，不写 L0 latest、L2 latest/历史、outbox 或配置修订。
- 普通页面只使用“原始数据”、“标准实体”和“数据来源与计算”，不暴露 L1、DAG、修订 UUID 或计划摘要。
- 不增加 Redis、Kafka、微服务、表、迁移、依赖或第二套公式引擎。
- 本计划只完成一条 PCS 数据纵向切片，不扩告警、JDM、控制或工作台功能。

---

### Task 1: 恢复与生产 Schema 一致的 PostgreSQL 试算验证环境

**Files:**
- Modify: `backend/tests/test_point_processing_postgres.py`
- Reference: `backend/tests/test_data_frames_postgres.py`
- Reference: `backend/tests/test_committed_frame_consumers_migration_postgres.py`

**Interfaces:**
- Consumes: Schema 044 节点数据主干、45 边缘保留、46 数据帧、47 outbox payload、48 帧保留、49 consumer receipt、50 L0 易用性、51 节点私有加工。
- Produces: 真实 PostgreSQL 测试夹具，能运行 `PostgresFrameRepository` 和 `PostgresPointProcessingTrialEvaluator`。

- [ ] **Step 1: 保留当前 RED 证据**

  运行单个试算用例，确认失败为 `UndefinedTable: t_data_frames`，而非公式断言失败。

- [ ] **Step 2: 让点位加工夹具按生产顺序应用 Schema 045–051**

  复用既有 migration test helper，不在测试中重写 DDL。目标顺序是：

  ```python
  _reset_through_043(cursor)
  _apply_044(cursor)
  _restore_timescale_001_footprint(cursor)
  _apply_045(cursor)
  _apply_046(cursor)
  _apply_047(cursor)
  cursor.execute(MIGRATION_048.read_text(encoding="utf-8"))
  cursor.execute(MIGRATION_049.read_text(encoding="utf-8"))
  cursor.execute(MIGRATION_050.read_text(encoding="utf-8"))
  cursor.execute(MIGRATION_051.read_text(encoding="utf-8"))
  ```

- [ ] **Step 3: 重跑单用例**

  Run: `ZIZU_POSTGRES_TEST=1 python -m pytest tests/test_point_processing_postgres.py::PointProcessingPostgresTest::test_local_l0_formula_loads_bound_tags_without_cross_entity_dependencies -vv`

  Expected: 进入真正的本地 L0 公式/试算断言，不再缺表。

### Task 2: 收口同节点多 L0 公式的唯一运行语义

**Files:**
- Modify: `backend/app/services/data_trunk_contracts.py`
- Modify: `backend/app/services/data_trunk_conversion.py`
- Modify: `backend/app/services/data_trunk_postgres.py`
- Modify: `backend/app/services/point_processing.py`
- Modify: `backend/tests/test_data_trunk_conversion.py`
- Modify: `backend/tests/test_point_processing.py`
- Modify: `backend/tests/test_point_processing_postgres.py`

**Interfaces:**
- Consumes: `InputReference(source_kind: "l0" | "l2", source_id)`、`evaluate_processing(...)`、ready `PointProcessingPlan`。
- Produces: 同节点公式将 L0 binding 编译为 `FormulaTransform.sources`；只有 L2 依赖进全站 DAG 表。

- [ ] **Step 1: 运行现有单元 RED/GREEN 用例并确认它们的变异点**

  如果把 formula source 强制改回 L2，以下用例必须失败：本节点 L0 求值、plan 编译、PostgreSQL binding 加载。

- [ ] **Step 2: 保留最小实现**

  `FormulaTransform` 允许 L0/L2；评估器按 reference kind 严格校验观测类型；公式时间和来源 ID 从实际 Raw/L2 observation 提取。

- [ ] **Step 3: 确认 DAG 不把 L0 伪装成实体依赖**

  PostgreSQL 断言本地多 L0 公式的 `t_point_processing_dependencies` 为 0，但加载的 transform 含两个 `InputReference.l0(...)`。

- [ ] **Step 4: 运行聚焦测试**

  Run: `python -m pytest tests/test_data_trunk_conversion.py tests/test_point_processing.py -q`

  Run: `ZIZU_POSTGRES_TEST=1 python -m pytest tests/test_point_processing_postgres.py -q`

  Expected: PASS，不出现 skip。

### Task 3: 使用同一引擎返回不持久化的已提交帧试算

**Files:**
- Modify: `backend/app/services/point_processing.py`
- Modify: `backend/app/services/point_processing_postgres.py`
- Modify: `backend/app/api/point_processings.py`
- Modify: `backend/tests/test_point_processing.py`
- Modify: `backend/tests/test_point_processing_postgres.py`
- Modify: `backend/tests/test_point_processing_public_api.py`

**Interfaces:**
- Produces: `PointProcessingTrial(frame_sequence, frame_time, configuration_revision, outputs)`。
- Produces: plan JSON 中的 `trial` 字段：成功时 `{available: true, ...}`，无法试算时 `{available: false, reason, message}`。

- [ ] **Step 1: 证明试算不应用 plan**

  单元测试使用真实 `PointProcessingService.trial(plan)`，断言输出值与帧身份，同时断言 repository 没有 application。

- [ ] **Step 2: PostgreSQL 试算只读一个基线修订的最新终态帧**

  使用 repeatable-read/readonly 事务读取 frame、L0 latest 和 L2 latest，然后在事务外调用 `evaluate_processing`。输出只包含实体身份、值、类型、单位、质量、原因、时间和来源 ID。

- [ ] **Step 3: API 不把无试算帧伪装成成功**

  static plan ready 但无 committed frame 时，`trial.available=false` 且带稳定 reason；不伪造 0 或 GOOD。

- [ ] **Step 4: 运行服务/API/PostgreSQL 测试**

  Run: `python -m pytest tests/test_point_processing.py tests/test_point_processing_public_api.py -q`

  Run: `ZIZU_POSTGRES_TEST=1 python -m pytest tests/test_point_processing_postgres.py -q`

### Task 4: 把试算结果变成实施工程师看得懂的发布门禁

**Files:**
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/components/data-trunk/InlinePointProcessingPanel.tsx`
- Modify: `frontend/src/components/data-trunk/inlinePointProcessingModel.ts`
- Modify: `frontend/src/components/data-trunk/inlinePointProcessingModel.test.mjs`
- Modify: `frontend/src/components/NodeTagPanel.tsx`
- Test: `frontend/src/components/data-trunk/inlinePointProcessingModel.test.mjs`

**Interfaces:**
- Consumes: `PointProcessingPlan.trial` API contract。
- Produces: “标准实体”页面术语以及试算值/单位/质量/时间/来源摘要。

- [ ] **Step 1: 写前端 RED 用例**

  用一个字面 trial fixture 断言展示模型产生“42 W / 质量正常 / 2 个来源”；`available=false` 产生中文不可试算说明，不是空白或假 0。

- [ ] **Step 2: 增加类型和纯展示模型**

  ```ts
  export interface PointProcessingTrialOutput {
    entity_instance_id: string
    entity_definition_id: string
    value: number | boolean | string | string[] | null
    data_type: string
    unit: string | null
    quality: number
    reason: string | null
    observed_at: string
    source_ids: string[]
  }
  ```

  质量固定映射 `192=正常` / `64=存疑` / `0=无效` / `1=超时`，未知值显示“未知质量”。

- [ ] **Step 3: 在“检查结果”后显示业务结果**

  ready + trial available 显示值、单位、质量、数据时间和来源数；trial unavailable 显示中文原因。发布继续只由 static plan blocker 决定，试算的 BAD/STALE 是现场质量证据而不是擅自改变配置合法性。

- [ ] **Step 4: 统一用户术语**

  节点页签改为“原始数据”和“标准实体”；发布成功提示不出现 L0/L1/L2。

- [ ] **Step 5: 前端验证**

  Run: `node --test src/components/data-trunk/inlinePointProcessingModel.test.mjs`

  Run: `npx tsc -b`

  Run: `npm run build`

### Task 5: 纵向切片门禁与状态记录

**Files:**
- Modify: `CODEX_HANDOFF.md`
- Modify: `README.md`
- No production deployment in this task.

**Interfaces:**
- Produces: 一份不夸大的已提交能力记录和下一个现场验收点。

- [ ] **Step 1: 运行聚焦后端门禁**

  Run: `python -m pytest tests/test_data_trunk_conversion.py tests/test_point_processing.py tests/test_point_processing_public_api.py -q`

  Run: `ZIZU_POSTGRES_TEST=1 python -m pytest tests/test_point_processing_postgres.py -q`

- [ ] **Step 2: 运行前端门禁**

  Run: `node --test src/components/data-trunk/inlinePointProcessingModel.test.mjs`

  Run: `npx tsc -b`，再运行 `npm run build`

- [ ] **Step 3: 运行 `git diff --check` 并审查变更边界**

  只纳入本计划源码、测试、现行规格和状态文档；不纳入用户的未跟踪部署文档或研究文档。

- [ ] **Step 4: 更新客观状态**

  README 区分“1号机正在运行”和“本地候选”；不在尚未现场验收时宣称工业交付就绪。

## Self-review

- Spec coverage: 直接实体、多 L0 公式、同引擎试算、原子发布、用户术语和来源证据均有对应任务。实体编辑/回退、模板库、告警消费和1号机部署属于后续独立纵向切片，本计划不伪装完成。
- Placeholder scan: 无 TBD/TODO/“类似任务”。
- Type consistency: `PointProcessingTrial` 从 Python API 到 TypeScript 使用同一字段名。
