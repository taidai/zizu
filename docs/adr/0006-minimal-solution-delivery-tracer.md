# ADR-0006：最薄解决方案交付模块与公开主缝

## Status

Superseded by ADR-0013 — 2026-08-25

维护者已确认本文的公开主测试缝，本文作为票据 01 的 TDD 契约。

## Context

ZiZu 当前没有解决方案包、安装计划、幂等安装或交付报告模块。现有
`backend/acceptance_f0_f3.py` 会直接查询数据库，只能作为迁移诊断，不能证明实施
工程师通过公开产品边界完成交付。

票据 01 必须建立一条最薄但真实的纵向切片：上传一个只含平台 liveness 验收项的
解决方案包，完整校验后生成可审查计划，幂等安装，运行验收并取得不可变机器报告。
后续参数、实体、告警、控制和 EMS 工作台都沿这条主缝扩展，不能另起内部捷径。

## Decision

### 1. 深模块接口

新增 `SolutionDelivery` 模块，外部接口只有四个命令：

```python
class SolutionDelivery:
    def import_package(self, archive: PackageArchive) -> PackageImport: ...
    def plan_install(self, command: PlanInstall) -> InstallationPlan: ...
    def apply_install(self, command: ApplyInstall) -> InstallationOutcome: ...
    def run_acceptance(self, command: RunAcceptance) -> DeliveryReport: ...
```

- `import_package` 在任何持久化前完成归档安全、清单、引用、摘要和平台兼容性校验；
  只保存验证通过的不可变包。
- `plan_install` 基于已保存包摘要与当前站点配置版本生成不可变计划；调用方不传入
  计划项或数据库动作。
- `apply_install` 只接受计划 ID、计划摘要和幂等键。模块拒绝过期、阻断或摘要不匹配
  的计划，并在一个事务内生成安装记录与新站点配置版本。
- `run_acceptance` 只接受已安装记录与幂等键；模块加载包携带的清单，调用允许的
  公开探针，保存并返回不可变报告。

查询由只读 HTTP 资源返回已保存的包、计划、安装和报告表示，不额外扩大命令接口。
HTTP Adapter 只处理 multipart/JSON、状态码和认证上下文，不包含校验、幂等或状态机
规则。

### 2. 最小包格式

票据 01 使用 ZIP 归档，至少包含：

```text
minimal-liveness.zizu.zip
├── solution.yaml
└── acceptance/
    └── liveness.yaml
```

`solution.yaml` v1 最小字段：

```yaml
schemaVersion: zizu.solution/v1alpha1
id: org.zizu.minimal-liveness
version: 1.0.0
displayName: Minimal liveness
platform:
  version: ">=0.4.77,<0.5.0"
assets:
  - id: acceptance.platform-liveness
    kind: acceptance
    path: acceptance/liveness.yaml
    sha256: "<64 lowercase hex>"
acceptance:
  - acceptance.platform-liveness
```

`acceptance/liveness.yaml` 只允许平台维护的声明式检查类型：

```yaml
schemaVersion: zizu.acceptance/v1alpha1
id: acceptance.platform-liveness
kind: platform_liveness
required: true
timeout: 5s
```

包不携带 Python、JavaScript、Shell、SQL 或任意模板表达式。票据 01 不引入参数或
其他资产种类。清单 YAML 使用仓库已有 PyYAML，解析后必须是普通映射/列表/标量；
禁用自定义 tag。

### 3. 不可变摘要与归档安全

包摘要不是原始 ZIP 字节摘要，因为 ZIP 时间戳和压缩器会造成同内容不同字节。模块
对规范化文件索引计算 SHA-256：路径按 UTF-8 字节排序，每项包含规范路径、文件长度
和文件内容 SHA-256；清单声明的每个资产摘要必须先匹配，随后对整个索引计算包摘要。

导入失败必须零写入。首版安全限额固定为：归档最大 10 MiB、最多 256 个文件、单个
解压文件最大 2 MiB、总解压大小最大 20 MiB、压缩比最大 100:1。拒绝绝对路径、
`..`、反斜杠歧义、重复/大小写折叠冲突路径、NUL、符号链接、硬链接、设备文件、
加密条目和未被清单引用的可执行扩展名。只读取内存或隔离临时目录，校验完成前不把
归档内容写入运行配置目录。

稳定机器码至少包括：

- `PACKAGE_ARCHIVE_UNSAFE`
- `PACKAGE_LIMIT_EXCEEDED`
- `MANIFEST_INVALID`
- `ASSET_REFERENCE_INVALID`
- `ASSET_DIGEST_MISMATCH`
- `PLATFORM_INCOMPATIBLE`
- `PACKAGE_DIGEST_CONFLICT`

相同包 ID、版本和内容摘要重复导入返回原包记录；相同 ID+版本但摘要不同返回
`PACKAGE_DIGEST_CONFLICT`，不得覆盖。

### 4. 计划、安装与幂等

票据 01 的计划只有包安装与 liveness 验收注册两项，仍使用通用计划表示：
`add / update / preserve / remove_candidate / conflict / blocked`。首装为 `add`；同一
摘要已安装为 `preserve`。计划保存包摘要、基础站点配置版本、计划项、阻断项和计划
摘要。

`apply_install` 必须同时满足：包仍存在且摘要一致、当前站点配置版本等于计划基础
版本、计划无阻断、提交摘要等于保存摘要。否则分别失败为
`PACKAGE_NOT_FOUND`、`INSTALL_PLAN_STALE`、`INSTALL_PLAN_BLOCKED` 或
`INSTALL_PLAN_DIGEST_MISMATCH`，且零写入。

幂等键在“命令类型 + 站点单例 + 调用主体”范围唯一，并保存请求摘要与结果引用：
相同键和请求摘要返回原结果；相同键不同请求返回 `IDEMPOTENCY_KEY_REUSED`。成功
事务创建安装记录、新的不可变站点配置版本和变更审计。重复计划或重复执行不增加
配置版本。

### 5. 验收与报告

新增最小匿名 `GET /api/v1/health/live`，只返回稳定 `status=alive` 与平台版本，不
复用当前包含组件状态的 `/health`，也不把后者的假绿当作 readiness。

`platform_liveness` 检查只能通过 `PublicApiProbe` Adapter 请求公开
`/api/v1/health/live`。生产 Adapter 使用实例自身公开基址；端到端测试使用 ASGI HTTP
Adapter，但仍穿过路由和 HTTP 表示，不调用 health 函数或数据库。后续检查类型使用
注册白名单扩展，解决方案包不能指定任意 URL 或代码。

验收报告保存：报告 ID、平台版本、包 ID/版本/摘要、安装与站点配置版本、执行主体、
开始/结束时间、每项状态/机器码/耗时/脱敏证据、总体状态与报告内容摘要。必需项失败
则总体 `failed`。liveness 成功码为 `PLATFORM_LIVE`；超时、非 2xx、响应契约错误分别
为 `LIVENESS_TIMEOUT`、`LIVENESS_HTTP_ERROR`、`LIVENESS_RESPONSE_INVALID`。

报告一经保存不可修改。相同安装、验收清单摘要和幂等键返回同一报告；重新实际运行
必须使用新幂等键并生成新报告，而不是覆盖历史。

### 6. 持久化与迁移

生产使用 Postgres Adapter 和新的迁移表，至少保存验证包、安装计划、安装记录、站点
配置版本、验收运行/报告和幂等记录。所有外键以 UUID 与不可变摘要约束；JSONB 只
保存经过 Schema 校验的规范表示，不把任意上传文本当作运行配置。

辅助契约测试可使用内存 Adapter；票据完成证据必须包含 Postgres 迁移测试和通过
公开 HTTP 的主测试。不能因为本机缺数据库而把内存 Adapter 当作生产完成证据。

## Test seam to confirm

开始 TDD 前，请维护者确认：

1. **主测试缝**：固定真实 ZIP → multipart 公开导入 API → 计划 API → 两次相同
   幂等安装 → 验收 API → 报告 API；断言第二次安装返回同一安装/站点配置版本，报告
   含 `PLATFORM_LIVE`。测试不调用内部对象或 SQL。
2. **失败主缝**：损坏/不兼容包导入失败且随后无法查询；过期计划执行失败且安装列表
   不变；liveness Adapter 返回超时/错误时报告必需项与总体均失败。
3. **辅助契约缝**：从 `SolutionDelivery` 四个命令断言机器码、摘要、计划和幂等
   不变量；可替换 Repository、Clock 和 PublicApiProbe，不断言 SQL 或私有函数。
4. **持久化证据**：在 Postgres 上从空 Schema 应用迁移并执行主缝，进程重建后仍能
   读取同一包、安装和报告。

不新增测试依赖。HTTP 主缝使用 FastAPI/HTTPX 现有依赖；ZIP、摘要与限额使用 Python
标准库；测试包不含现场参数或凭据。

## Consequences

- 后续票据得到稳定的包、计划、安装、站点版本和报告骨架，可纵向扩展而无需新增
  内部捷径。
- 即使最小票据也需要真实归档安全和 Postgres 持久化，工作量高于简单 CRUD，但避免
  把不可逆的错误包格式和假验收固化为公共契约。
- `SolutionDelivery` 内部实现可拆分解析、仓储、检查器注册表等内部模块；这些不是
  HTTP 调用方需要学习的额外接口。
- 当前 `/health` 继续作为兼容诊断；新 `/health/live` 才是最小存活契约，readiness
  将在后续安全/发布票据中独立收口。
