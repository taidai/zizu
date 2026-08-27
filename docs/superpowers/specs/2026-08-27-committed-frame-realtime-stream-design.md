---
status: implemented
date: 2026-08-27
supersedes:
  - legacy L0 polling WebSocket
  - legacy per-entity L2 WebSocket
---

# 提交后数据帧实时流设计

## 1. 目的与范围

本规格完成数据帧底座的第二阶段：让节点详情中的 L0 原始点位和 L2 全局实体只显示同一张已经提交
的数据帧。数据库是真相来源，WebSocket 只是送达方式；事务 B 未提交的数据不得出现在页面。

本轮交付：

- 当前节点的一次性 L0/L2 完整终态快照；
- 快照游标之后的统一 committed frame WebSocket 增量；
- 有界补帧、重复去除、严格顺序和游标过旧重读；
- 节点 L0/L2 实时界面接入；
- 旧 L0 数据库轮询和旧 L2 独立实时通道删除；
- 已发布 outbox 与 L0/L2/帧明细的固定保留策略。

本轮不改 EMS 工作台、告警、JDM、控制和业务画面。它们后续复用相同 committed L2 seam，不再建立
旁路。本轮不增加 Redis、Kafka、全站广播、客户端逐帧确认或现场可配置的保留策略。

## 2. 已选择方案

采用“一个深模块、一次快照、一条数据帧流、当前节点订阅”。拒绝以下方案：

- 保留两个旧 WebSocket：L0/L2 会出现时序错位；
- 每个浏览器接收全站数据：接口简单但浪费网络和浏览器资源；
- WebSocket 先显示、数据库随后补写：页面可能展示不可恢复的半成品；
- 追求 exactly-once：需要客户端确认和持久会话状态，复杂度高于收益。

送达语义固定为 ordered at-least-once：允许重发，绝不倒序；客户端按帧序号幂等应用。

## 3. 深模块与 seam

新增 `CommittedFrameStream` 深模块。REST、WebSocket、节点界面和后续上层应用只跨这一处 seam，不得
直接读取 outbox 或自行拼接 L0/L2。

模块对调用者只表达两个动作：

```text
read_snapshot(scope) -> Snapshot
subscribe_after(scope, cursor) -> ordered FrameDelta | ResnapshotRequired
```

`scope` 首版支持一个 `node_id`。模块内部保留按 `tag_ids` 和 `entity_instance_ids` 解析订阅集合的能力，
但不把数据库表、claim token、published 状态或 hub 生命周期暴露给调用者。未来 EMS 工作台可用明确的
L2 实体集合复用该接口，无须修改帧协议。

模块内部负责：

- 同一数据库切面读取快照；
- 游标编码、scope 绑定和校验；
- durable replay 与进程内 live buffer 的无缝衔接；
- 过滤当前订阅范围；
- 帧排序、去重和断档判断；
- outbox 清理边界。

PostgreSQL adapter 与内存 adapter 是该 seam 的两个真实 adapter；生产与契约测试通过相同接口验证。

## 4. 快照契约

公开入口为：

```text
GET /api/v1/runtime/frame-snapshot?node_id=<uuid>
```

服务端在一个只读、可重复读事务中完成以下动作：

1. 解析当前节点的活动 L0 点位和活动 L2 实体；
2. 读取该事务切面可见的最大终态 `frame_sequence`；
3. 读取不晚于该帧的 L0/L2 latest；
4. 返回完整当前状态和绑定该 scope 的不透明游标。

响应至少包含：

```json
{
  "type": "frame_snapshot",
  "node_id": "uuid",
  "cursor": "opaque",
  "frame_sequence": 123,
  "frame_time": "2026-08-27T10:00:00Z",
  "configuration_revision": 46,
  "l0": [],
  "l2": []
}
```

每条 L0 返回稳定身份、类型化值、单位、source/effective quality、数据时间、接收时间、accepted beat、
来源和自身最后变化帧号。每条 L2 返回稳定身份、类型化值、单位、quality/reason、数据/计算时间、来源
摘要、点位加工修订、配置修订和自身最后变化帧号。没有观测的活动项仍出现在完整快照中，值为空且
质量为 STALE/等待数据，不得因“尚无数据”从界面消失。

游标编码版本、全局帧号与 scope 摘要。客户端不得解析游标；把一个节点的游标用于另一个节点时稳定
返回 `FRAME_CURSOR_SCOPE_MISMATCH`。

## 5. WebSocket 契约

公开入口为：

```text
WS /api/v1/ws/data-frames
```

客户端继续使用现有短时、单次 WebSocket ticket。认证成功后发送：

```json
{
  "subscribe": {
    "node_id": "uuid",
    "after": "opaque-cursor"
  }
}
```

服务端按帧发送一条原子消息：

```json
{
  "type": "frame_delta",
  "cursor": "new-opaque-cursor",
  "frame_id": "uuid",
  "frame_sequence": 124,
  "status": "COMPLETE",
  "frame_time": "2026-08-27T10:00:01Z",
  "configuration_revision": 46,
  "l0_changes": [],
  "l2_changes": [],
  "failure": null
}
```

一个帧不得拆成多个可独立应用的 WebSocket 消息。FAILED 帧同样送达，其 L0、L2 STALE 结论和失败
事实来自同一终态提交。若全站产生了新帧但当前节点没有变化，发送只含帧头和新游标的轻量 checkpoint，
使长连接的恢复位置持续前进。

`l0_changes` 与 `l2_changes` 中单项的字段形状分别与快照中的 L0/L2 单项一致；增量不是只有 value 的
简写，质量、时间、来源和修订必须随同一帧一起更新。

`CommittedFrameStream` 先登记 live buffer，再读取 durable replay 到固定高水位，最后按帧号合并 replay
和 buffer；同一帧只向客户端交付一次。这样快照完成后到 WebSocket 建立前、replay 读取期间以及 live
切换瞬间都不会形成丢帧窗口。

## 6. outbox 与补帧

`t_data_frame_outbox` 继续保持每个终态帧一行，但增加不可变的版本化 delta payload。payload 只保存该
帧的 L0/L2 变化、帧头和失败事实，不保存全站完整快照。事务 B 在完成帧时一次生成 payload；dispatcher
和 replay 直接读取它，不在每次连接或补帧时逐帧回扫 L0/L2 历史。

dispatcher 把完整帧交给进程内 hub 后才标记 `published_at`。没有在线浏览器也算 hub 接收成功，因为
断线客户端随后从 durable outbox replay。发布失败保留既有有界退避和 claim fencing；不得跳过队头。

补帧窗口固定为：

- 已发布记录最长一小时；
- 同时最多保留最新 5000 个已发布帧；
- 两个条件任一达到即可清理；
- 未发布或仍被 claim 的记录永不按时间清理。

游标早于最老可补帧序号时，WebSocket 发送：

```json
{
  "type": "resnapshot_required",
  "code": "FRAME_CURSOR_TOO_OLD"
}
```

随后正常关闭该订阅。客户端重新调用快照接口，不做无限重试。

## 7. 客户端状态机

节点选择或首次打开：

```text
LOADING_SNAPSHOT -> CONNECTING -> LIVE
                           |          |
                           +-- retry -+
```

规则：

1. 先取得完整快照并一次替换当前节点投影；
2. 再携带快照游标建立 WebSocket；
3. `frame_sequence <= applied_sequence` 的重复/旧消息直接忽略；
4. 新帧先在内存构造下一投影，再一次提交 React state；
5. 收到 `FRAME_CURSOR_TOO_OLD`、scope 不匹配或协议版本不支持时，清空连接状态并重新取快照；
6. 普通断线指数退避重连，最大等待 5 秒；重连沿用最后成功应用的游标；
7. 切换节点时取消旧请求、关闭旧 socket，并用 request generation 防止旧响应覆盖新节点。

客户端不发送逐帧 ACK，不持久保存游标到 localStorage；页面刷新直接取得新快照。

## 8. 界面要求

节点详情的 L0 与 L2 都显示：

- 当前类型化值与单位；
- GOOD、UNCERTAIN、BAD、STALE；
- 数据时间、接收时间和帧时间；
- 当前帧号和配置修订。

L0 额外显示采集来源；L2 额外显示点位加工来源和来源证据。STALE 保留最后已知值，但用灰色样式和
明确文案标为陈旧；不得把有值的 STALE 显示成在线。历史趋势继续使用历史读取接口，不经 WebSocket
重放七天数据。

本轮不重新设计节点页视觉结构，只替换数据获取和状态应用逻辑。旧 1.5 秒 L0 latest 轮询、旧
`/ws/telemetry` 和旧 `/ws/entity-observations` 在新节点页接入并通过契约测试后硬删除，不保留双流 fallback。

## 9. 七天明细保留

固定策略为：

- L0 秒级明细保留 7 天，延续 Schema 045 的 6 小时压缩；
- L2 秒级明细保留 7 天；
- 与秒级明细关联、且没有故障/审计等长期证据引用的数据帧保留 7 天；
- L0 继续使用既有小时与日汇总；L2 数值型实体增加小时/日的首值、末值、最小、最大、平均和各质量
  计数，布尔/字符串/枚举实体增加首值、末值和各质量计数；汇总长期保留；
- outbox 独立按一小时/5000 帧清理。

数据帧不能按时间直接整批删除。受控维护函数必须先处理到期 L2/source 明细，再只删除已经没有 L0、
L2、失败或其他证据引用的终态帧。现有“普通会话禁止删除终态帧”和 append-only 门禁继续生效；只有
受测的数据库维护路径可以清理。被长期故障或审计证据引用的稀疏帧可超过七天保留。

L0/L2 latest 投影本身不参加七天清理，当前值、质量、时间、修订和摘要继续可读；其原始秒级历史仍按
七天到期。故障、审计或统计结果显式引用的稀疏原始观测与帧例外保留。因此“七天”是普通秒级历史
的上限，不是破坏当前状态或长期证据的强制级联删除期限。

任何清理失败只记录系统故障并等待下次运行，不得阻塞采集、处理或 outbox 发布，也不得用级联删除
破坏长期证据。

## 10. 权限与失败规则

- 快照和 WebSocket 均要求 `runtime.read`；scope 中的节点及实体由服务端重新解析，客户端不能靠传 ID
  越权读取；
- HTTPS/WSS 和现有开发模式例外沿用现行身份规则；
- 数据库不可用时快照明确失败，WebSocket 不发送伪造缓存；
- payload 无法解码、帧号断档或配置修订非法时 fail closed，记录系统故障并要求重新快照；
- 单个慢客户端使用固定 64 帧的有界队列；队列溢出时关闭该连接并要求重新快照，不拖慢 outbox
  dispatcher；
- 告警、JDM和控制仍不得在本轮偷接 WebSocket，它们后续从 committed L2 consumer seam 接入。

## 11. 验收标准

实现必须同时证明：

1. 快照中的全部 L0/L2 不晚于同一个终态帧游标；
2. 快照期间并发提交的新帧能通过 replay 到达，快照与 live 切换零丢帧；
3. 同帧 L0/L2 由客户端一次应用，不出现可见半帧；
4. COMPLETE 与 FAILED 帧都只在事务 B 提交后送达；
5. 断线重发不会倒退投影，重复帧不产生第二次可见更新；
6. 当前节点过滤正确，其他节点 payload 不泄露；无本节点变化时 checkpoint 仍推进游标；
7. 一小时/5000 帧内可补发；游标过旧稳定要求重新快照；
8. 未发布 outbox 不被清理，发布顺序和队头 fencing 不回归；
9. 旧 L0 轮询与旧 L2 独立流已删除，源码中不存在节点实时数据的第二条运行路径；
10. 7 天清理不删除仍被 L0/L2/失败/审计引用的帧，普通会话仍不能删除终态证据；
11. 节点切换、快速切换、断线、慢客户端和页面刷新均不会让旧节点数据覆盖新节点；
12. 前端显示值、质量、三类时间、帧号、配置修订和来源，STALE 保值但机器与界面都不把它当 GOOD；
13. 完整后端、真实 PostgreSQL 迁移/补帧/保留测试、前端类型检查与生产构建通过；
14. 发布门禁解除 `COMMITTED_FRAME_CONSUMER_MISSING` 与 `DATA_FRAME_RETENTION_POLICY_UNRESOLVED`，但
    在本轮完成前继续 fail closed。

## 12. 实施顺序

1. 先以测试冻结快照、游标、delta payload、replay 和保留契约；
2. 演进 Schema，为 outbox 增加 immutable payload，并增加受控 L2/帧清理；
3. 实现 `CommittedFrameStream` 及 PostgreSQL/内存 adapter；
4. 接入 dispatcher、hub、快照 REST 和统一 WebSocket；
5. 改造节点 L0/L2 客户端状态机和界面字段；
6. 删除旧轮询与旧双 WebSocket；
7. 跑完整回归、真实数据库门禁和前端构建，不在本规格内部署 1 号机。

## 13. 最终结论

ZiZu 的实时读取固定为“数据库先原子提交完整数据帧，`CommittedFrameStream` 再提供一次完整快照和
有序 at-least-once 增量”。节点页只订阅当前节点，断线在一小时/5000 帧内补发，超过范围重读快照；
L0/L2/帧秒级明细固定保留七天，outbox 短期保留。该方案用一个深模块隐藏一致性、补帧和清理复杂度，
不增加新中间件，也不给页面或上层应用留下旁路。

## 14. 实现记录

已在 `ticket/v0.4.85-node-data-trunk-hard-cut` 完成实现，核心提交为 `09734bf` 至 `8c3baa0`；
数据库 Schema 为 048。唯一公开实时读取接口为 `GET /api/v1/runtime/frame-snapshot` 与
`WS /api/v1/ws/data-frames`，旧 L0/L2 实时 WebSocket、前端连接器和未使用面板均已删除。
