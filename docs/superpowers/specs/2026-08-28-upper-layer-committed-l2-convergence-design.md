---
status: accepted
date: 2026-08-28
authority:
  - 2026-08-27-zizu-platform-core-architecture-design.md
  - ADR-0014
  - ADR-0015
---

# 上层模块收口到 committed L2 专项规格

## 1. 目标

把告警、JDM、控制和固定 EMS 工作台全部收口到同一条已提交 L2 数据缝。事务 B 未提交的结果、L0、
原始 MQTT、独立 latest 轮询和旧配置修订不得驱动任何上层业务。四个模块按“告警 → JDM → 控制 →
工作台”逐个形成可独立验收的纵向切片，不同时铺开。

本规格不增加消息中间件、微服务、规则引擎、页面设计器或通用工作流；不实现统计实体；不执行真实设备
写入。每个切片通过测试和现场只读验收后，才进入下一切片。

## 2. 当前基线

基线为 `v0.4.85-rc.9`、提交 `a3e20a2`、Schema 048。实时黑板、数据帧事务 A/B、恢复、统一帧
outbox、REST 快照、WebSocket 帧增量和节点 L0/L2 实时页已经实现并部署 1 号机。

当前仍有四类差距：

1. 帧 outbox 只发布给 `CommittedFrameStream`，告警没有生产消费循环；
2. `TagAlarmAdapter`/`MqttAlarmAdapter` 仍表达 L0/MQTT 告警入口，`EntityAlarmAdapter` 会重新查询 latest；
3. 告警配置应用直接推进配置修订，没有经过现有 `ConfigurationRuntimeGate` 排空旧帧/outbox；
4. JDM、自动控制和 EMS 工作台仍各自读取实体运行态，尚未绑定来源帧与活动配置修订。

## 3. 唯一公共缝

保留 `FrameOutboxDispatcher` 作为唯一有序队头，新增一个很薄的 `CommittedFrameFanout`：

```text
t_data_frame_outbox 队头
  → 告警 committed L2 消费者
  → JDM committed L2 消费者
  → 自动控制意图消费者
  → CommittedFrameStream（节点页/EMS 工作台）
  → 全部成功后 published_at
```

消费者接口只有一个动作：

```python
class CommittedFrameConsumer(Protocol):
    async def publish(self, event: FrameOutboxEvent) -> None: ...
```

fanout 按注册顺序串行调用，不并发、不吞异常、不拆帧。任一消费者失败时，本帧保持未发布，沿用既有
指数退避；后续帧不得越过队头。采集、事务 A/B 和历史保存不回滚，也不因上层失败停止。

## 4. 状态型消费者幂等

Schema 049 新增共享收据表：

```text
t_committed_frame_consumer_receipts
  consumer_key       TEXT
  frame_id           UUID → t_data_frames(frame_id) ON DELETE CASCADE
  frame_sequence     BIGINT
  configuration_revision BIGINT
  consumed_at        TIMESTAMPTZ
  PRIMARY KEY (consumer_key, frame_id)
  UNIQUE (consumer_key, frame_sequence)
```

告警、JDM 与自动控制各使用固定 `consumer_key`。消费者在同一业务事务内先尝试插入收据，再写状态、
审计和模块 outbox；任一步失败则整笔回滚。重放已提交帧时收据冲突，消费者返回幂等成功且不再次改变
业务状态。收据随到期数据帧级联删除，不形成永久账本。

消费者必须校验帧配置修订与当前活动修订相同。因为所有配置发布都先排空未发布 outbox，正常情况下
二者恒等；不一致表示旧版本遗留或数据损坏，必须 fail closed，不能用新定义解释旧帧。

## 5. 切片一：告警

新增 `CommittedL2AlarmConsumer`，只遍历 `FrameOutboxEvent.l2_changes`。每个变化项按稳定
`entity_instance_id` 找到当前告警定义，并提交统一 `AlarmRuntime`。告警观测固定记录：

- `source_kind=committed_l2`；
- `source_ref=frame:<frame_id>/entity:<entity_instance_id>`；
- 帧 ID、帧序号、配置修订、L2 event ID、节点、单位、质量、reason、加工修订和来源摘要；
- L2 的 `observed_at`；缺失时整帧消费失败，不以系统当前时间伪造。

GOOD 的数值才能满足触发或恢复条件；UNCERTAIN/BAD/STALE 只推进“仍活动/清除 pending”等 fail-closed
路径。数据帧已经显式产生质量变化，因此正式入口不再用直接 latest 适配器的时间间隔猜测连续性。

同一帧的全部告警观察、事件转换、审计、通知 outbox 和消费收据必须在一个 PostgreSQL 事务中提交。
`AlarmRuntime` 增加批量事务入口，原有单条 `submit` 继续供状态机单元测试和暂存的 JDM 告警动作使用。

告警配置 `apply` 复用点位加工已有的 `ConfigurationRuntimeGate`：开始发布前排空采集/处理中的旧帧与
未发布 outbox；配置事务失败时取消栅栏；成功后重建黑板活动修订并进入 WARMING。旧告警定义只能解释
旧帧，新定义只能从新修订首帧开始工作。

硬切删除 `tag_mqtt_alarm_adapter.py` 和 `entity_alarm_adapter.py` 及其旧契约测试；原始告警 MQTT 继续
由采集管道明确忽略。历史 `t_alarms` 只读档案不属于运行消费者，本切片不迁移或扩展它。

## 6. 切片二：JDM

JDM 消费者从同一帧组装本次 committed L2 输入，只运行活动配置修订绑定的已发布模型。试运行与正式
执行共用同一个模型 adapter；正式执行输出判断或控制意图，不直接写告警表、控制表或设备。每次执行
记录模型 ID/版本、帧 ID/序号、配置修订、输入实体及质量。任何必需输入非 GOOD、缺失或属于其他修订
时拒绝执行，并留下稳定原因码。旧的定时 latest 扫描在验收后硬删除。

## 7. 切片三：安全控制

人工和 JDM 继续提交同一种控制命令。自动意图必须携带来源帧、来源配置修订和 JDM 模型证据。真正写
Neuron 前重新读取活动修订和目标 committed L2，校验 GOOD、来源帧未倒退、唯一 L0 控制绑定、限值、
联锁、权限和幂等；任一失败即终止，不下发。接口响应只表示命令已接收，只有后续新 committed L2 在
容差和超时内回读到目标状态才成功。真实设备写入需要维护者单独授权。

## 8. 切片四：EMS 工作台

工作台通过 `CommittedFrameStream.read_snapshot(scope)` 取得明确 L2 实体集合的完整快照，再以同一游标
接收帧增量。页面不自行查询实体 latest，不订阅第二条 WebSocket，不读取 L0。能流、SOC、功率、状态、
趋势入口和告警摘要必须展示质量、帧时间与配置修订；STALE 保留最后值但明确灰显，不得呈现为在线。

## 9. 配置与失败语义

- 配置计划仍只读；apply 前重新校验计划摘要与基础修订。
- 任一上层配置发布都走同一运行栅栏；不得绕过或另建锁。
- consumer 重试不得生成重复告警转换、重复 JDM 意图或重复控制命令。
- consumer 失败不阻止新 L0/L2 入库，但阻止 outbox 越序、配置切换和机器动作。
- 重启后从最老未发布帧继续；已写收据的状态型消费者幂等跳过。
- 收据、业务状态和业务审计必须同事务；单独写收据或事后补收据均不允许。

## 10. 验收矩阵

每个切片至少证明：

1. 事务 B 提交前消费者不可见，提交后按帧序可见；
2. 同一帧重放两次，业务状态和副作用只发生一次；
3. 处理一半崩溃后整笔回滚，重启可完整重放；
4. 非 GOOD、旧配置修订、缺字段和无定义均 fail closed；
5. 一个帧包含多个实体/规则时全部成功或全部回滚；
6. 配置发布在旧帧/outbox 未排空时零写失败，排空后切换并进入 WARMING；
7. 没有 L0/MQTT/latest 旁路，也没有新增依赖、服务或模块私有帧队列；
8. 1 号机保持 host 网络与 `/dev/mqueue`，容器 healthy、restart=0；不做 Caddy/TLS 或真实设备写入。

## 11. 交付顺序

本规格拆成四份独立实施计划。当前只执行第一份“告警 committed L2 消费者”；通过本地契约测试、
PostgreSQL 集成测试和 1 号机只读验收后，再写 JDM 计划。每个切片只构建一次最终候选固定摘要，避免
为中间修复重复发布大镜像。
