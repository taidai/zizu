---
status: pending-review
date: 2026-09-02
authority:
  - 2026-08-27-zizu-platform-core-architecture-design.md
  - 2026-08-28-minimal-alarm-center-design.md
  - ADR-0004
  - ADR-0015
supersedes:
  - 2026-08-28-minimal-alarm-center-design.md#9 中“本轮不做外部通知渠道”的范围限制
---

# 告警 HTTP 通知设计

## 1. 目的

让平台管理员在界面中定义可复用的 HTTP 请求，让实施工程师为告警规则选择一个通知目标，并在告警发生
或恢复后可靠地请求指定 Webhook。ZiZu 不直接实现邮件、短信、飞书或企业微信协议；这些系统只需提供
HTTP 接收地址。

本设计扩展最简告警中心，不改变数据主干、告警匹配和状态机语义：

```text
committed L2
  → CommittedL2AlarmConsumer
  → AlarmRuntime
  → 告警状态转换 + 告警通知任务（同一数据库事务）
  → 提交后 HTTP 投递
  → Webhook / Node-RED / 飞书 / 企业微信 / 其他接收系统
```

## 2. 成功标准与非目标

### 2.1 成功标准

1. 平台管理员不修改源码或 SQL，即可配置、测试、启停和删除 HTTP 通知配置；
2. 实施工程师可在一条告警规则上选择零个或一个已启用的 HTTP 通知配置；
3. 告警发生和现场恢复各产生至多一个告警通知任务，人工确认不产生通知；
4. Webhook 断网、超时或返回错误不得阻塞或回滚告警状态；
5. 通知可重试、可追踪、可手工重发，进程重启后结果仍然确定；
6. 敏感请求头不明文回显、不写日志，数据库泄露时不能直接得到其明文；
7. 部署后由无头浏览器和真实临时 HTTP 接收端证明配置、绑定、发生、恢复和失败链可用。

### 2.2 非目标

- 不建设邮件、短信、飞书或企业微信专用发送器；
- 不建设通用流程编排器，不支持请求链、条件分支、响应字段提取或后续动作；
- 不允许 JavaScript、Python、Jinja 或其他任意代码；
- 不引入 Redis、Kafka、新微服务或第二套规则引擎；
- 不让 HTTP 结果反向修改告警、JDM、控制或 L2；
- 不补发功能上线前已经存在的告警事件；
- 不执行设备写入或控制命令。

## 3. 已确认的产品决策

| 主题 | 决策 |
| --- | --- |
| 请求能力 | 支持方法、URL、查询参数、请求头、Content-Type、请求体模板和超时 |
| 配置复用 | 系统工具维护多个有名称的 HTTP 通知配置 |
| 规则绑定 | 每条告警规则可不选，最多选择一个配置 |
| 发送时机 | 告警发生和现场恢复发送；人工确认不发送 |
| 发送结果 | 与告警状态完全解耦 |
| 成功判定 | 任意 HTTP 2xx 为成功；不解析响应以驱动业务动作 |
| 重试 | 首次立即发送，失败后 5 秒、30 秒、5 分钟重试，共最多四次尝试 |
| 幂等 | 每个状态转换产生稳定通知 ID，并作为 HTTP 幂等键发送 |
| 启用门禁 | 当前配置内容必须测试成功后才能启用 |
| 修改语义 | 待发送与重试任务在下一次尝试时使用修改后的当前配置 |
| 删除语义 | 删除时解除所有规则绑定并取消全部未完成通知；已完成历史保留 |
| 上线语义 | 只处理上线后新产生的状态转换，不扫描或补发旧告警 |
| 密钥 | 敏感请求头加密保存，读取接口只返回“已配置” |

修改语义和删除语义是维护者明确选择的即时生效模型。其代价是同一通知的不同尝试可能发往不同地址，
删除配置也会主动放弃尚未完成的投递。系统必须用逐次尝试记录把该行为完整呈现，不能伪装成原请求未变。

## 4. 领域与模块设计

### 4.1 领域对象

**HTTP 通知配置**是可复用的出站请求定义。它属于系统级安全配置，不是 L2、告警规则内容、JDM 模型
或节点树对象。

**告警通知绑定**把一个不可变告警定义版本关联到零个或一个 HTTP 通知配置。绑定由现有告警配置计划
和 apply seam 发布；它不扩大告警规则的运行输入。

**告警通知任务**由 `ALARM_ACTIVATED` 或 `ALARM_RECOVERED` 状态转换产生。它与转换同事务持久化，只有
提交后才能投递。投递结果从不写回告警事件。

**通知尝试**记录某次实际 HTTP 请求的目标、时间和结果。由于已确认“当前配置立即影响待发送任务”，
同一告警通知任务的不同尝试允许记录不同的请求方法和目标地址。

### 4.2 深模块与接口

新增一个深模块 `AlarmHttpNotifications`，把模板渲染、密钥解密、HTTP 请求、重试、并发领取、状态转换
和安全日志隐藏在模块内部。外部接口只暴露以下能力：

```text
AlarmHttpNotifications
├─ test(configuration_id) → HttpNotificationTestResult
├─ dispatch_due(limit) → HttpNotificationDispatchResult
└─ retry(notification_id, actor) → NotificationResult
```

`AlarmRuntime` 不直接构造 HTTP 请求。它只继续通过持久化 Adapter 在同一事务中记录状态转换与通知任务。
配置管理接口也不得自行复制模板渲染或 HTTP 成功判断，测试发送和正式发送必须穿过相同内部 seam。

### 4.3 数据流

```text
配置：系统工具填写请求 → 保存草稿 → 模拟告警测试 → 测试摘要匹配 → 启用
绑定：告警规则选择配置 → 预览计划 → apply → 不可变定义 + 可撤销绑定
运行：状态转换 → 同事务写事件/转换/通知任务 → commit → 后台投递
失败：非 2xx/超时/网络错误 → 记录尝试 → 等待重试 → 成功或最终失败
修改：请求配置变化 → 自动停用 → 重新测试启用 → 未完成任务下次使用新配置
删除：解除绑定 + 取消未完成任务 + 删除配置；告警规则和告警事件继续有效
```

## 5. HTTP 请求契约

### 5.1 可配置字段

- 名称与可选说明；
- 方法：`GET`、`POST`、`PUT`、`PATCH`、`DELETE`；
- 绝对 URL，仅允许 `http` 或 `https`；
- 查询参数键值列表，每项可标记为敏感；
- 请求头键值列表，每项可标记为敏感；
- Content-Type；
- UTF-8 请求体模板；
- 超时时间，允许 1～30 秒，默认 5 秒；
- 启用状态。

Webhook URL 本身经常携带访问令牌，因此完整 URL 与敏感查询参数也按密钥处理：数据库加密保存，列表、
日志和尝试记录只显示脱敏地址。现场局域网和本机 HTTP 地址是单站边缘部署的合法目标，因此不得一律
禁止私网或 loopback。只有具备
系统管理权限的平台管理员能够创建、测试、修改、启停或删除配置。必须拒绝 URL 用户信息、`file:`、
`ftp:` 等非 HTTP 协议以及链路本地元数据地址。

### 5.2 模板变量

首版只支持固定白名单变量，不支持表达式、函数或条件：

```text
notification.id       event.id               event.type
event.time
alarm.name            alarm.severity         alarm.state
alarm.definition_id   alarm.rule_key
node.id               node.name              node.path
entity.id             entity.key              entity.name
entity.value          entity.unit             entity.quality
entity.observed_at
```

`event.id` 是告警事件 ID，`notification.id` 是本次状态转换对应的稳定通知任务 ID；`event.type` 只可能为
`ALARM_ACTIVATED`、`ALARM_RECOVERED` 或测试时的 `TEST`。系统以 `notification.id` 自动附加
`Idempotency-Key` 与 `X-ZiZu-Notification-Id`，用户配置不得覆盖。

当 Content-Type 为 JSON 时，变量替换值按 JSON 值编码，模板中的变量不加引号，例如：

```json
{
  "notification_id": {{notification.id}},
  "event_id": {{event.id}},
  "type": {{event.type}},
  "alarm": {{alarm.name}},
  "value": {{entity.value}}
}
```

其他 Content-Type 使用普通 UTF-8 文本替换。保存时检查变量名；测试和正式发送前都检查渲染结果。
JSON 模板渲染后不是合法 JSON 时，请求不得发出，并记录稳定错误码。

### 5.3 成功与响应

- HTTP 2xx 视为成功；3xx 不自动跟随，防止密钥被重定向到其他地址；
- 4xx、5xx、连接失败、DNS 失败和超时均视为失败；
- 不从响应正文提取字段，不触发规则、JDM 或控制；
- 只保存经过控制字符清理且不超过 4 KiB 的响应摘要；
- 日志和公开接口不得返回解密后的敏感请求头、完整认证信息或完整请求体。

## 6. 配置生命周期与界面

### 6.1 系统工具 → HTTP 通知

列表展示名称、方法、脱敏目标地址、启用状态、最后测试时间和结果，并提供新增、编辑、测试、启停和删除。

编辑表单依次展示：

1. 名称、方法、URL、Content-Type、超时；
2. 查询参数和请求头，两者的值均可勾选“敏感信息”；
3. 请求体模板与可点击插入的变量目录；
4. 隐藏密钥后的最终请求预览；
5. “发送测试”结果，包括状态码、耗时和响应摘要。

测试使用固定模拟告警，不创建告警事件或告警通知任务。测试成功时保存当前配置内容摘要；只有
`tested_digest == current_digest` 才能启用。修改方法、URL、参数、请求头、Content-Type、请求体或超时
后自动停用并清除测试通过状态。未重新启用期间，关联规则继续告警，未完成通知等待而不消耗尝试次数。

编辑已有 URL、敏感查询参数或敏感请求头时只显示“已配置”或脱敏摘要。输入框留空表示保留原值，只有
明确点击“清除”才删除；输入新值表示替换。请求预览和测试结果始终隐藏敏感值。

删除操作按已确认语义执行：同一事务解除全部绑定、把未完成通知标记为 `cancelled`，随后删除配置。
已完成通知和通知尝试历史不删除。

### 6.2 告警规则

告警规则表单新增可选的“HTTP 通知”下拉框，只列出已启用且测试摘要仍有效的配置，最多选择一个。
不选择不阻止试算、预览或发布。选择或更换通知目标属于告警配置变更，必须继续经过现有计划、摘要、
并发复核和 apply seam；不得新增旁路保存接口。

### 6.3 告警中心 → 通知记录

通知记录展示告警名称、发生/恢复、节点、实体、目标、状态、尝试次数、状态码、错误原因和时间。状态固定为：

```text
pending → retry_wait → delivered
                     ↘ failed
pending/retry_wait/failed → cancelled（删除配置）
failed → pending（手工重发）
```

最终失败允许手工重发。手工重发继续使用当前配置并追加尝试记录；告警状态不变。配置已经删除时不能重发，
页面明确显示“通知配置已删除”。

## 7. 持久化设计

### 7.1 HTTP 通知配置

新增 `t_alarm_http_notification_configs`，至少包含：

```text
id, name, description, method, encrypted_url, url_display,
public_query_params, encrypted_secret_query_params,
public_headers, encrypted_secret_headers, content_type, body_template,
timeout_seconds, current_digest, tested_digest, tested_at,
last_test_status, enabled, created_at, updated_at
```

名称在单站内唯一。公开查询只返回脱敏 URL、普通参数、敏感参数名称、普通请求头、敏感请求头名称和
`configured=true`，永不返回密文或明文。

### 7.2 告警通知绑定

新增 `t_alarm_http_notification_bindings`：

```text
definition_id PRIMARY KEY, configuration_id, created_at, created_by
```

绑定表独立于不可变告警定义，使删除配置能够按已确认语义解除关联，而不修改历史定义内容。

### 7.3 告警通知任务

扩展现有 `t_alarm_notification_outbox`，至少增加：

```text
transition_id, transition_code, configuration_id,
status, attempt_count, next_attempt_at,
last_http_status, last_error_code, last_error_detail,
last_response_excerpt, delivered_at, cancelled_at, updated_at
```

`transition_id` 唯一，保证同一状态转换重复消费时不产生第二个通知任务。删除配置时只把未完成任务取消并
清空其配置引用；历史事件、转换和已完成任务保持不变。

新增 `t_alarm_notification_attempts`，逐次追加：

```text
id, notification_id, attempt_no, attempted_at,
method, target_display, duration_ms, outcome,
http_status, error_code, error_detail, response_excerpt
```

尝试记录不保存完整 URL、查询密钥、请求头、其他密钥或完整请求体。通知历史首版随告警事件保留，
不增加独立可配置清理系统。

### 7.4 密钥

敏感请求头使用现有 Python 加密运行库，在应用层以独立 `HTTP_NOTIFICATION_ENCRYPTION_KEY` 加密。部署脚本
生成高熵密钥并注入运行环境；数据库只存认证密文。备份恢复必须同时恢复该运行密钥，否则相关配置失败
关闭并要求管理员重新录入敏感值。密钥缺失时，创建敏感请求头或启用相关配置返回明确错误，不能降级明文。

## 8. 投递与恢复语义

后台投递循环使用数据库领取与租约，支持多协程或进程重入而不并发领取同一任务。每次尝试前重新读取
当前配置，因此配置修改会按已确认语义立即影响下一次尝试。配置暂时停用时任务保持等待，不增加尝试次数；
配置删除时任务被取消。

现有 `notification_throttle_seconds` 继续只约束相邻告警事件的发生通知。同一告警事件只有已生成发生通知
任务时才生成配对的恢复通知；恢复通知不再单独节流，避免出现只有恢复、没有发生的孤立消息。

尝试计划为：

```text
尝试 1：任务提交后立即
尝试 2：失败后 5 秒
尝试 3：再次失败后 30 秒
尝试 4：再次失败后 5 分钟
之后：failed，可手工重发
```

投递保证为至少一次。进程可能在对端已接收、但本地尚未标记成功时崩溃，因此接收方应使用系统提供的
幂等键去重。ZiZu 不以分布式事务或“恰好一次”承诺掩盖这一事实。

启动时只继续处理迁移完成后生成的 `pending`、`retry_wait` 任务。现有 outbox 历史行在迁移时标为不需
投递的历史记录，不扫描活动告警，不制造上线通知风暴。

## 9. 权限与稳定错误

- 平台管理员：HTTP 通知配置 CRUD、测试、启停、删除；
- 实施工程师：在告警规则中选择已启用配置、查看非敏感发送状态；
- 业主操作员：查看与其告警权限一致的通知状态，不查看请求配置和密钥；
- 手工重发需要告警管理权限并记录操作者。

稳定错误码至少包括：

```text
HTTP_NOTIFICATION_NOT_FOUND
HTTP_NOTIFICATION_DISABLED
HTTP_NOTIFICATION_NOT_TESTED
HTTP_NOTIFICATION_TEST_STALE
HTTP_NOTIFICATION_INVALID_URL
HTTP_NOTIFICATION_INVALID_TEMPLATE
HTTP_NOTIFICATION_SECRET_KEY_NOT_CONFIGURED
HTTP_NOTIFICATION_DELIVERY_TIMEOUT
HTTP_NOTIFICATION_DELIVERY_REJECTED
HTTP_NOTIFICATION_DELIVERY_CANCELLED
```

前端必须把这些错误翻译为可执行的中文提示，不能只显示“请求未完成”。

## 10. 公开接口

系统管理接口：

```text
GET    /api/admin/alarm-http-notifications
POST   /api/admin/alarm-http-notifications
PUT    /api/admin/alarm-http-notifications/{id}
DELETE /api/admin/alarm-http-notifications/{id}
POST   /api/admin/alarm-http-notifications/{id}/test
POST   /api/admin/alarm-http-notifications/{id}/enable
POST   /api/admin/alarm-http-notifications/{id}/disable
```

告警接口：

```text
GET  /api/alarms/notification-deliveries
POST /api/alarms/notification-deliveries/{id}/retry
```

现有告警配置输入增加可空 `http_notification_config_id`。省略或传 `null` 表示不通知，保持现有配置兼容；
不存在、未启用或测试摘要失效的配置必须阻止计划生成或 apply。

## 11. 测试与验收

### 11.1 模块测试

- 变量白名单、JSON 值编码、文本渲染、未知变量和非法 JSON；
- URL、方法、超时、参数和请求头校验；
- URL、敏感查询参数和敏感请求头的加密、解密、密钥缺失、接口遮蔽和日志脱敏；
- 2xx 成功、3xx/4xx/5xx、DNS、连接和超时失败；
- 固定重试时序、领取租约、进程重启、并发领取与手工重发；
- 状态转换重复消费只生成一个通知任务；
- 发生和恢复通知，确认与重复采样不通知；
- 通知失败不修改告警事件状态。

### 11.2 PostgreSQL 与公开接口测试

- 配置 CRUD、测试摘要、启停门禁和角色权限；
- 告警计划绑定零个或一个配置，计划摘要和 apply 结果一致；
- 配置修改后未完成任务下一次读取新配置，并在尝试表留下真实目标；
- 删除配置后绑定清零、未完成任务取消、已完成历史保留；
- 旧 outbox 行不被补发，重启只恢复新模型未完成任务；
- 响应摘要截断，数据库和接口均不出现完整 Webhook URL、敏感查询参数或敏感请求头明文。

### 11.3 前端测试

- 系统工具列表、编辑器、变量插入、预览、测试、启停和删除；
- 内容修改后测试状态失效并自动停用；
- 告警规则只能选择一个有效配置，也可以不选；
- 通知记录状态、错误、尝试历史和手工重发；
- TypeScript 检查、前端测试和生产构建通过。

### 11.4 1 号机验收

1. 部署前备份数据库和运行密钥，保持 host network 与 `/dev/mqueue`；
2. 启动一个临时只记录请求的 HTTP 接收端，不接入控制或设备写链；
3. 无头浏览器沿“节点树 → L0 → L1 → L2 → 告警”确认主干未回归；
4. 在系统工具创建 HTTP 通知，完成预览、测试和启用；
5. 发布一条绑定该配置的无控制告警规则；
6. 用受控 L2 输入完成发生和自然恢复，接收端分别收到一次可解释请求；
7. 人工确认不产生通知；重复帧不产生重复通知任务；
8. 分别模拟非 2xx 与超时，证明重试、最终失败、手工重发和告警状态独立；
9. 修改地址后证明下一次尝试使用新地址；删除配置后证明绑定解除且待发送任务取消；
10. 清理临时规则、节点、通知配置和接收端，确认后端 healthy、restart 0、错误日志为空。

未完成上述真实 Browser 与接收端闭环时，交付结论只能是 `INCOMPLETE` 或 `FAILED`。

## 12. 完成定义

- 数据库迁移、后端深模块、公开接口和三处界面均已实现；
- 告警状态机仍只消费 committed L2，通知只来自已提交状态转换；
- 没有同步 HTTP 阻塞告警事务，没有 JDM/Node-RED 旁路和第二套通知语义；
- 所有专项、完整后端测试、前端测试和生产构建通过；
- 1 号机使用固定镜像摘要部署并完成第 11.4 节验收；
- 发布锁定、部署证据、公开接口和密钥恢复方法已写入文档。
