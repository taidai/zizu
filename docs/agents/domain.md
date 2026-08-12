# Domain Docs

## Before exploring

开始代码探索、设计或实现前，读取：

- 仓库根目录 `CONTEXT.md`；
- 与任务相关的 `docs/adr/` 决策记录；
- `docs/product-destination.md`，当任务涉及产品范围、配置式交付或 EMS 参考交付时。

缺少某个文件时继续工作；只有在术语或不可逆决策真正形成时，才由领域建模流程创建相应文档。

## Layout

本仓库使用单一领域上下文：

```text
/
├── CONTEXT.md
└── docs/
    └── adr/
```

现有历史决策位于 `docs/decisions/`；新建满足 ADR 条件的决策时使用 `docs/adr/`，并在相关文档中链接历史决策。

## Vocabulary

Issue 标题、规格、测试名称和界面文案应使用 `CONTEXT.md` 定义的术语。若现有术语不足，先通过领域建模澄清，不以近义词悄悄引入第二套语言。

## ADR conflicts

若拟议工作与现有 ADR 冲突，必须明确指出冲突及重新开启决策的原因，不得静默覆盖。
