# ADR-0007：统一控制命令运行时接口

## Status

Accepted — 2026-08-14

维护者已确认 ZiZu 的目的地是“简单配置即可交付 EMS 的工业 IoT 平台”。本文固定票据
08 的控制命令契约与测试缝；票据 09、10、11 分别迁移兼容设备写入、规则/策略和删除
旁路。

## Context

现有全局实体写入、Neuron 写入和规则动作会直接调用设备适配器。设备 HTTP 返回成功只
表示请求被接受，既没有统一的限值、联锁、冷却、幂等和审计，也不能证明现场设备已经
达到期望状态。它不符合配置式 EMS 交付所需的“控制下发/回读”验收，也会让人工、规则
和策略形成语义不同的写入旁路。

票据 06、07 已提供唯一、可确认的实体实例来源。控制必须以该实体实例为目标，不能再
引用全局实体名称、点位优先级或 Neuron 地址。

## Decision

### 1. 配置契约

可写的 `entity_definition` 可以声明一个可选 `control` 对象；没有该对象的 `W`/`RW`
实体仍不可控制。控制配置是解决方案包资产的一部分，并在实体实例安装时随实例一起
持久化，因而包升级、计划和审计均可追溯。

```yaml
control:
  minimum: -1000
  maximum: 1000
  cooldown: 5s
  readback:
    definition: pcs.activePower
    tolerance: 0.1
    timeout: 15s
  interlocks:
    - definition: bms.ready
      equals: true
  highRisk: false
```

- `minimum`、`maximum` 仅用于 `INT`/`FLOAT`，且最小值不大于最大值；枚举、布尔和
  字符串没有数值限值。
- `readback.definition` 是同一设备实例内的实体定义；它可以等于目标定义。回读实体和
  目标必须具有兼容的数据类型与单位。`tolerance` 仅用于数值类型，`timeout`、
  `cooldown` 是正整数秒。
- 每个联锁引用同一设备实例的读实体定义，且仅支持精确 `equals` 比较。联锁观测必须
  新鲜且质量 GOOD；缺失、陈旧或质量差均拒绝控制。
- `highRisk: true` 要求一次短期二次确认。确认只绑定主体和规范化命令内容，不包含
  明文凭据；任何目标、值或策略变更都使确认失效。

### 2. 深模块接口

新增 `ControlCommandRuntime`，所有新控制消费者仅依赖它：

```python
class ControlCommandRuntime:
    def request_confirmation(self, request: SubmitControlCommand) -> ControlConfirmation: ...
    def submit(self, request: SubmitControlCommand) -> ControlCommand: ...
    def reconcile(self, command_id: UUID) -> ControlCommand: ...
    def get(self, command_id: UUID) -> ControlCommand: ...
```

- `submit` 接受主体、来源类型、实体实例 ID、期望值、幂等键和可选确认 ID。它解析唯一
  `ResolvedEntitySource`，按服务端类型/限值/设备可控性/联锁/持久冷却/幂等顺序校验，
  再把已授权请求交给唯一的设备执行 Adapter。
- 设备 Adapter 只接受运行时已经验证的分派请求；它不接受 HTTP 主体、实体名称或任意
  Neuron 地址。首版生产 Adapter 根据确认 `tag_id` 读取受控点位目录后调用 Neuron；
  测试 Adapter 经协议模拟器发布回读观测。
- `reconcile` 只读取声明的回读实体实例。观察时间必须不早于 `dispatched_at`；数值按
  容差比较，其他类型精确比较。它可被请求完成后、后台恢复和验收重复调用。
- `get` 返回稳定公开命令表示，不泄露 Neuron 地址、下游凭据或原始适配器错误。

### 3. 状态与不变量

命令创建、每次状态转换和关联审计均持久化。主流程为
`accepted → validated → dispatched → readback_confirmed`；终态为 `rejected`、
`timeout`、`failed`、`mismatch`。状态只能前进，终态不得回到执行态。

- 写 Adapter 抛错进入 `failed`；写返回不代表成功。
- 期限内取得新鲜、GOOD 且与期望不符的回读进入 `mismatch`；没有可确认回读直到期限
  到达进入 `timeout`。
- `(actor, idempotency_key)` 唯一且绑定规范化请求摘要。相同摘要返回原命令；不同摘要
  拒绝为 `IDEMPOTENCY_KEY_REUSED`。
- 冷却以目标实体实例为作用域并由数据库行锁保留；服务重启、并发请求或新运行时实例
  不能绕过它。重启后 `dispatched` 命令继续回读，过期后安全结束为 `timeout`。
- 拒绝同样形成不可变命令与审计证据，但不调用设备 Adapter。高风险确认一次性消费。

### 4. 公开 HTTP 边界

票据 08 新增：

- `POST /entity-instances/{id}/control-confirmations`：请求短期确认；
- `POST /entity-instances/{id}/control-commands`：提交命令，必须带 `Idempotency-Key`；
- `GET /control-commands/{id}`：读取命令状态；
- `POST /control-commands/{id}/reconcile`：触发一次安全回读检查。

它们使用既有 `control.write` 能力（admin、engineer、operator），并保留独立的命令
审计。Ticket 09 已把旧 `/neuron/write` 和 `/devices/{node_id}/rpc` 变为兼容入口：

- Neuron 三段地址仅在能够唯一映射到已确认、可控实体实例时接受；不能映射时创建明确
  拒绝证据，绝不把任意地址传给下游 Adapter。
- RPC 不再接收任意 MQTT topic/payload；新调用方必须给出与节点一致的实体实例 ID 和目标值。
  受限旧形态只允许 `command` 精确匹配已确认实体实例的定义 ID，并只读取
  `payload.value`；`topic` 与 QoS 从不参与路由或执行。
- 两种兼容响应都返回控制命令及迁移提示。`201` 只表示命令已被处理，调用方必须以命令
  状态 `readback_confirmed` 作为现场成功的唯一依据。

遗留 `/entities/{id}/write` 以及规则动作仍由票据 10、11 迁入，避免在兼容期假称设备写入
已经成功。

### 5. 持久化

迁移新增控制配置、命令、状态转换、幂等键、冷却保留和二次确认表。命令与状态转换为
追加式证据；业务账户只具备新增/读取权限。PostgreSQL Adapter 使用事务和行锁完成
幂等、冷却与确认消费，运行期状态永不依赖进程内缓存。

## Test seam to confirm

1. **控制辅助缝**：只通过 `ControlCommandRuntime` 断言公开命令、机器码和状态单调性。
   固定实例注册表、内存命令仓储、记录型分派 Adapter 与观测 Adapter 证明类型/限值/
   联锁、幂等、冷却、分派失败、回读确认/不一致/超时和高风险确认；不断言 SQL 或
   私有调用顺序。
2. **公开交付缝**：通过登录后的公开 HTTP 导入带可控实体定义的解决方案包，安装并确认
   实体实例，提交控制命令；协议模拟器在设备边界发布回读，命令查询变为
   `readback_confirmed`。同一测试还证明无确认的高风险请求、权限/限值/联锁拒绝、
   幂等和审计。真实 PostgreSQL/Uvicorn 主缝额外证明进程重启后命令、冷却和在途回读仍
   可恢复，测试不直接写命令/观测数据库表。

不新增测试依赖；首版使用固定时钟、内存 Adapter 和现有 PostgreSQL 隔离主缝。

## Consequences

- 人工、已迁移的 Neuron/RPC 兼容 API、规则、策略和验收将共享唯一且可审计的控制语义；
  新消费者不能再直接写物理地址。
- 解决方案包获得可配置但受限的控制声明，实施工程师无需改平台源码或 SQL 即可交付
  基础控制；更复杂的联锁表达式、审批流和自动切换必须通过后续 ADR 扩展，不能借助
  任意脚本绕过此运行时。
- 票据 08 不代表现场已经可安全上线：TLS、固定制品、凭据轮换和发布锁定仍是独立的
  发布门禁。
