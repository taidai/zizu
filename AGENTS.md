# AGENTS.md

## Project

ZiZu 是面向工业控制系统交付的开源物联网低代码平台，光储充 EMS 是首个参考交付。项目使用 FastAPI、React、TimescaleDB、NanoMQ 与 Neuron。

## Working rules

- 使用中文与维护者沟通。
- 开始工作前阅读 `CONTEXT.md`、相关 ADR 和 `CODEX_HANDOFF.md`。
- 遵循现有代码风格，优先小而聚焦的改动。
- 添加依赖前先询问。
- 不覆盖或提交与当前任务无关的工作树改动。
- 后端改动至少运行相关测试，提交前运行完整测试套件。
- 前端改动至少运行 `npm run build`。
- 新增配置格式、部署步骤或公开接口时同步更新文档。
- 凭据、客户参数与现场真实拓扑不得进入仓库。
- 每次声称开发或部署完成前，使用 Browser 按“节点树 → L0 原始点位 → L1 点位加工 → L2 全局实体 → 告警”实际操作；具体步骤见 `docs/acceptance-checklist.md`。登录或任一环节未走通时，结论必须是 `INCOMPLETE` 或 `FAILED`。

## Agent skills

### Issue tracker

议题、规格与 Wayfinder 决策地图使用 GitHub Issues。参见 `docs/agents/issue-tracker.md`。

### Triage labels

使用默认五角色分诊标签。参见 `docs/agents/triage-labels.md`。

### Domain docs

使用单一领域上下文：仓库根目录 `CONTEXT.md` 与 `docs/adr/`。参见 `docs/agents/domain.md`。
