# Minimal Delivery Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用一份目标、一份清单和一个命令，诚实判断 ZiZu 当前改动是否达到可验收状态。

**Architecture:** 文档保存目标与人工验收口径；一个仅使用 Python 标准库的仓库脚本依次运行现有后端、脚本、前端测试与构建，并可对指定站点执行匿名只读存活检查。脚本输出单个 JSON 报告；本地检查失败为 `FAILED`，未提供现场地址为 `INCOMPLETE`，全部通过为 `PASSED`。

**Tech Stack:** Python 3.12 标准库、unittest、Node.js test runner、npm/Vite

**Spec:** `docs/development-target.md`、`docs/acceptance-checklist.md`

## Global Constraints

- 不引入新依赖、插件、服务、表或 API。
- 现场检查只允许匿名 GET，不做登录、配置、控制或设备写入。
- 不提交凭据、客户参数或现场地址。
- 不把缺失的现场证据写成通过。

---

### Task 1: 固定目标和验收口径

**Files:**
- Create: `docs/development-target.md`
- Create: `docs/acceptance-checklist.md`

- [ ] **Step 1:** 写明唯一产品主干、当前优先级和明确不做项。
- [ ] **Step 2:** 把自动检查与必须人工操作的业务闭环分开，规定“缺证据不得通过”。

### Task 2: 实现单命令验收

**Files:**
- Create: `scripts/test_verify_delivery.py`
- Create: `scripts/verify_delivery.py`

**Interfaces:**
- Consumes: 仓库现有 unittest、前端 `*.test.mjs`、`npm run build`、匿名 `/api/v1/health/live`。
- Produces: `python scripts/verify_delivery.py [--site-url URL]`，stdout 为 JSON，退出码 0 仅表示 `PASSED`。

- [ ] **Step 1:** 先测试 `summarize_status()` 对失败、缺少现场检查和全通过的判定，以及 `validate_liveness()` 对版本不一致的拒绝。
- [ ] **Step 2:** 运行 `python -m unittest scripts.test_verify_delivery -v`，确认因实现缺失而失败。
- [ ] **Step 3:** 用标准库实现最短命令编排、版本/Schema/commit 记录和只读现场检查。
- [ ] **Step 4:** 重跑专项测试，确认通过。

### Task 3: 完整验证与交接

**Files:**
- Modify: `CODEX_HANDOFF.md`

- [ ] **Step 1:** 运行脚本专项测试、全部 scripts 测试及 `git diff --check`。
- [ ] **Step 2:** 运行新的单命令验收；没有提供现场地址时必须诚实得到 `INCOMPLETE`，同时各本地检查为 `PASSED`。
- [ ] **Step 3:** 更新交接记录，记录实际证据与仍需人工执行的清单。
