# Issue tracker: GitHub

本仓库的议题、规格和决策票据使用 GitHub Issues，所有操作通过 `gh` CLI 完成。

## Conventions

- 创建：`gh issue create --title "..." --body "..."`
- 读取：`gh issue view <number> --comments`
- 列表：`gh issue list --state open --json number,title,body,labels,assignees`
- 评论：`gh issue comment <number> --body "..."`
- 标签：`gh issue edit <number> --add-label "..."`
- 关闭：`gh issue close <number> --comment "..."`

仓库由当前工作树的 `origin` 自动推断。Pull Requests 不作为需求分诊入口。

## Publishing and fetching

- 当技能要求“发布到 issue tracker”时，创建 GitHub Issue。
- 当技能要求“读取相关 ticket”时，运行 `gh issue view <number> --comments`。

## Wayfinding operations

- **Map**：一个带 `wayfinder:map` 标签的 Issue，正文包含 Destination、Notes、Decisions so far、Not yet specified 与 Out of scope。
- **Child ticket**：优先使用 GitHub Sub-issues 关联到 Map；若仓库未启用 Sub-issues，则在 Map 使用任务列表，并在子票据顶部写 `Part of #<map>`。
- **Ticket labels**：`wayfinder:research`、`wayfinder:prototype`、`wayfinder:grilling`、`wayfinder:task`。
- **Blocking**：优先使用 GitHub 原生 issue dependencies；不可用时在票据顶部使用 `Blocked by: #<n>, #<n>`。
- **Frontier**：Map 中尚未关闭、没有未完成阻塞项、且无人认领的首个子票据。
- **Claim**：工作开始前执行 `gh issue edit <n> --add-assignee @me`。
- **Resolve**：向票据发布结论、关闭票据，并在 Map 的 Decisions so far 中加入一条指向该票据的摘要链接。

## Safety

- Issue 中不得粘贴凭据、客户参数、现场真实拓扑或其他现场私有数据。
- 安全漏洞只写风险和修复状态，不复述泄露值。
