# 本机 Demo 对齐与 1 号机一次发布 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Each lane works in its own worktree, produces a focused commit, and never deploys independently.

> 执行状态（2026-09-08）：Tasks 1–6 均已完成；v1.0.4 已由固定 ARM64 制品一次部署到1号机，部署后健康、只读 Headless 主干与可见 Browser 抽查通过，DB／NanoMQ 未重启。最终事实入口：`docs/reviews/2026-09-07-tablet-production-acceptance.md`。

**Goal:** 以 `http://127.0.0.1:19100/` 本机 Demo 为交互验收样板，将正式 ZiZu 补齐为简洁可用的运行首页、设备监控、工程主干、告警与通用 JDM；全部本地合并验收后，仅一次部署到 1 号机。

**Architecture:** 不复制 Demo 假数据或内存逻辑。正式系统继续使用真实节点树 → L0 原始点位 → L1 点位加工 → L2 全局实体 → 告警／调度策略／控制／固定 EMS 工作台；Demo 只约束信息层级、交互和视觉。三条独立开发路并行，D0 负责公共路由、来源证据、整合、版本和最终发布。

**Tech Stack:** 现有 React 18、TypeScript、Tailwind 3、原生 GoRules JDM、Playwright、FastAPI、PostgreSQL／TimescaleDB、NanoMQ、Neuron；不新增依赖，不变更 Schema。

**Spec:** `docs/superpowers/specs/2026-09-07-tablet-full-delivery-design.md`。本计划只收完该规格尚未交付的部分；冲突时以规格和最新 ADR 为准。

## 不可变边界

- 目标版本 `v1.0.4`，Schema 保持 `062`；各并行路不得自行改版本、推送、部署、发送通知或下发设备控制。
- Demo 的虚构能流、角色切换、内存保存、模拟回读和模拟通知不得进入正式应用。
- 上层只消费 committed L2；网络、权限或质量不明时 fail closed；“接口成功”不等于设备成功。
- 保留现有 dirty 文件；只提交本计划点名文件。所有改变先 RED 后 GREEN。
- 1 号机在所有本地门禁通过前不再部署；最终只替换 ZiZu 容器，保留 `network_mode: host`、`tmpfs: /dev/mqueue`，不重启数据库和 NanoMQ。

## 并行任务

### Task 1（P 路）：解除提交帧只读接口阻塞

**Files:** `backend/app/services/committed_frame_stream_postgres.py`、`backend/tests/test_committed_frame_stream_postgres.py`，必要时只增对应迁移索引及迁移测试。

- [x] 先用查询计时与 `EXPLAIN (ANALYZE, BUFFERS)` 定位 `/api/v1/runtime/frame-snapshot` 超过 30 秒的具体 SQL；不得凭猜测改代码。
- [x] 写出能复现慢查询形态或无界扫描的 RED 测试，特别检查全表 backlog 统计、L0 历史关联和 L2 latest 关联。
- [x] 做最小修复：查询必须按已有索引有界读取；如果索引缺失，只补必要索引，不引入缓存或新服务。
- [x] 跑 `python -m pytest backend/tests/test_committed_frame_stream_postgres.py backend/tests/test_committed_frame_public_api.py -q`，记录修复前后同一数据量的耗时。
- [x] 独立提交，交付根因、SQL 证据、测试命令和 commit。

### Task 2（R 路）：接入正式设备监控

**Files:** `frontend/src/pages/DeviceMonitorPage.tsx`、`frontend/src/components/runtime-monitoring/runtimeModel.ts`、同目录样式和测试、`frontend/e2e/tablet-devices.spec.ts`。公共 `App.tsx` 由 D0 修改。

- [x] 在共同基线上依次应用既有已审实现 `a6105b0`、`5b9d84e`、`461e628`；逐个解决冲突，不整分支合并。
- [x] 保证列表只来自真实节点与 committed L2，支持类型／名称／告警筛选，六卡一页，详情含不少于十条实体并可查历史与来源；传输中断时显示不确定且禁控制。
- [x] 跑 runtime model、设备页 Playwright 专项及 `npm run build`。
- [x] 独立提交，交付规格自查、测试命令和 commit；不得修改 `App.tsx`。

### Task 3（A 路）：补齐告警表格与通用原生 JDM

**Files:** `frontend/src/pages/AlarmCenterPage.tsx`、`frontend/src/pages/DispatchStrategyPage.tsx`、`frontend/src/components/alarm-center/*`、`frontend/src/components/dispatch-strategy/*`、`frontend/e2e/tablet-applications.spec.ts`。

- [x] 在共同基线上依次应用既有已审实现 `0225c42`、`5119b3f`、`4cda324`；逐个解决冲突，保留共同基线新增的 WebSocket、导航和真实 fixture。
- [x] 告警记录采用 10／20 行表格，支持本页全选、批量确认和真实详情／状态轨迹；加载失败不得对旧选择执行操作。
- [x] 调度策略保持“选择多个 L2 输入 → 编辑原生 JDM 决策表 → 指定多个可控 L2 输出”，保存完整 graph 和服务端草稿；不得退回固定“时段＋SOC”规则。
- [x] 跑告警／JDM 模型测试、应用页 Playwright 专项及 `npm run build`。
- [x] 独立提交，交付规格自查、测试命令和 commit；不得修改 `App.tsx`。

## 汇合任务（D0 串行）

### Task 4：补足 L2 人能看懂的来源证据

**Files:** `frontend/src/components/runtime-monitoring/EntityRuntimeDetail.tsx`、其模型／组件测试；仅在现有 API 字段不足时修改 `frontend/src/api/committedFrameStream.ts` 和对应测试。

- [x] RED：实体详情必须显示“本实体 ← L1 加工修订 ← 实际 L0 输入”，并列出值、质量、数据时间、接收时间和来源路径；缺证据时明确写“来源证据不可用”，不能假装正常。
- [x] 使用现有来源证据返回值完成最小展示；不得新增独立溯源系统。
- [x] 跑专项测试和 `npm run build`。

### Task 5：统一整合与本机真实验收

**Files:** `frontend/src/App.tsx`、冲突测试、`VERSION`、`backend/app/VERSION`、`docs/reviews/2026-09-07-tablet-production-acceptance.md`。

- [x] 依次合入 P、R、A 三个已验证 commit；D0 将 `monitor` 路由接到 `DeviceMonitorPage`，保持管理员／工程师／操作员现有权限边界。
- [x] 版本升到 `1.0.4`，Schema 不变；生成并校验前后端版本一致。
- [x] 跑全部后端测试、前端模型测试、`npm run build`；随后启动正式 FastAPI＋PostgreSQL＋JDM 本机环境。
- [x] 用无头浏览器在 1280×800 和 1024×768 沿主干验收：真实节点 CRUD → L0 导入／刷新／实时／历史 → L1 加工及跨节点 L2 输入 → L2 实时／历史／来源 → 告警配置／试算／启停／记录 → 通用 JDM 草稿／试算；只读验证控制页，不下发真实设备。
- [x] 逐项对照本机 Demo 的布局和操作，允许正式系统因无真实数据显示空态，禁止为了截图注入假数据。
- [x] 由独立审查者检查规格、过度设计、安全边界和回归；所有 P0/P1 清零后才允许打包。

### Task 6：固定产物、GitHub 与 1 号机一次发布

- [x] 从通过门禁的同一 commit 构建唯一 ARM64 镜像，记录 git SHA、镜像摘要、版本、Schema 和测试报告；先完成可恢复备份与隔离恢复验证。
- [x] 将最终分支快进／合入 `main` 并推送 GitHub；确认远端 commit 与制品标签完全一致。
- [x] 只执行一次 1 号机容器替换；固定镜像摘要，沿用旧容器 host network 和 `/dev/mqueue` 配置，不申请 TLS，不清理非目标数据。
- [x] 部署后先读 health、版本、容器重启数和 DB／NanoMQ 启动时间，再按现场安全边界用无头浏览器复验只读主干；最后用可见 Browser 抽查关键路径。
- [x] 发布门禁未失败；旧固定镜像和运行参数仍保留为回退依据，未现场热改后冒充通过。

## 完成定义

只有同时满足以下条件才算交付：本机 Demo 的核心交互已由真实数据和真实 API 实现；节点→L0→L1→L2→告警／JDM 主干可由界面独立完成；提交帧快照在目标数据量下及时返回；三角色、两种平板尺寸和失败关闭通过；GitHub、镜像摘要、1 号机运行 commit 一致；一次部署后的现场无头验收和 Browser 抽查均通过。
