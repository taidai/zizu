# ADR-0004：统一告警状态机模块与迁移接口

## Status

Accepted — 2026-08-13

2026-08-28 补充：来源 Adapter 的旧决策已由
[`ADR-0015`](0015-committed-l2-consumer-fanout.md) 取代。生产告警唯一入口现为
已提交 L2 帧；L0、MQTT 与 latest 适配器已硬删除。本文保留状态机、确认和持久化
不变量。

2026-09-04 补充：通知去重以一次事件的状态转换 ID 为边界。同一未恢复事件的重复
观测不重复通知；事件恢复后再次发生会创建新事件，并始终产生新的发生通知，不再被
上一事件的时间窗口抑制。`notification_throttle_seconds` 暂保留为兼容字段，不参与
跨事件抑制。

维护者已确认本文的外部接口与测试缝。实现仍按票据依赖顺序推进。

## Context

v0.4.77 的告警中心已有列表、筛选、确认、人工恢复和等级配置，但运行语义分散：

- `alarm_processor.py`、`tag_alarm_engine.py`、`entity_alarm_engine.py` 和
  `rule_engine.py` 都直接插入或恢复 `t_alarms`。
- MQTT、标签和实体路径在每次活动采样时执行
  `alarm_count = alarm_count + 1`，把采样次数误当作事件次数；线上已有活动告警
  `alarm_count=107392`。
- `PUT /alarms/{id}/resolve` 与告警中心“恢复”按钮允许人工伪造现场恢复。
- 旧表无法表达 `pending`、定义版本、恢复候选时间、状态转换证据或通知节流。
- 当前实体是全局定义而非站点实体实例，不能安全区分多台同类 PCS/BMS；因此本
 迁移依赖票据 06 及
 [`ADR-0005`](0005-entity-instance-registry-and-deterministic-binding.md) 的实体实例 ID。

继续分别修补四条路径会复制生命周期并保留语义分叉。需要把复杂度集中到一个深
模块，并让来源代码退化为 Adapter。

## Decision

### 1. 模块与外部接口

新增 `AlarmRuntime` 模块。其外部接口只有两个命令：

```python
class AlarmRuntime:
    def submit(self, observation: AlarmObservation) -> AlarmOutcome: ...
    def acknowledge(self, command: AcknowledgeAlarm) -> AlarmOutcome: ...
```

`submit` 的调用方只需知道：

- `definition_id`：版本化告警定义的稳定 ID；
- `entity_instance_id`：现场实际对象的稳定 ID；
- `observed_at`：现场观测时间；
- `value`、`quality` 与脱敏 `evidence`；
- `source_kind` 与 `source_ref`，用于审计来源，不参与生命周期分支。

调用方不传状态、不传事件 ID、不决定是否创建/恢复/通知，也不直接写告警表。
模块按 `definition_id` 加载不可变定义版本，定义包含触发条件、触发持续时间、迟滞/
恢复条件、恢复持续时间、严重度和通知节流。

`acknowledge` 只包含事件 ID、操作者主体、时间和可选备注。它不接受目标状态。

`AlarmOutcome` 返回事件 ID、当前状态、发生的转换（无转换时为 `None`）、机器原因码
和是否产生通知任务。来源 Adapter 不根据结果再次修改生命周期。

### 2. 状态与不变量

状态固定为 `normal`、`pending`、`active_unacknowledged`、
`active_acknowledged`、`recovered`，允许的转换为：

```text
normal ──触发候选──> pending ──持续达标──> active_unacknowledged
  ^                     │                        │          │
  └────条件提前消失─────┘                        │确认      │现场恢复
                                                v          │
                                      active_acknowledged ─┘
                                                │
                                                └──现场恢复──> recovered
```

- `normal` 是无活动事件的逻辑状态；首次触发观测创建 `pending` 事件。
- `pending` 达到触发持续时间才进入 `active_unacknowledged`；条件提前消失则关闭
  候选并回到 `normal`，不计为活动告警事件。
- 确认只允许 `active_unacknowledged → active_acknowledged`；重复确认幂等。
- `active_unacknowledged` 与 `active_acknowledged` 都可在现场恢复条件持续满足后进入
  `recovered`；确认不是恢复的前置条件。
- 活动状态下达到恢复条件后仍保持活动，同时记录 `recovery_candidate_since`；持续
  达标才进入 `recovered`。不额外引入 `recovering` 公共状态。
- `recovered` 为终态；同一定义再次触发会创建新事件，而不是复用旧事件。
- 同一 `definition_id + entity_instance_id` 最多一个 pending/活动事件，由数据库
  部分唯一索引与事务共同保证。
- 同一连续故障只对应一个事件；采样次数可作为诊断指标，但不得修改事件次数。
- 通知只由状态转换产生；重复采样不得发送通知。去重键为状态转换 ID；恢复后再次
  发生属于新事件，必须重新通知。
- 人工命令永远不能进入 `recovered`。不存在手工 create/resolve 的生产接口。

### 3. 持久化模型

采用 expand–migrate–contract，不猜测性重写 `t_alarms` 历史：

- `t_alarm_definitions`：稳定 ID、版本、实体实例、条件、持续时间、恢复/迟滞、严重
  度、通知节流、启用状态和内容摘要。
- `t_alarm_events`：定义 ID/版本、实体实例、状态、pending/active/ack/recovered 时间、
  恢复候选时间、首末观测、触发与恢复证据。
- `t_alarm_transitions`：事件、前后状态、时间、原因码、观测证据或操作者主体与备注。
- 通知任务使用 outbox，与事件转换同一事务提交；发送结果不反向改变告警状态。

旧 `t_alarms` 在兼容窗口只读。新 API 明确标记 `model_version`，查询层可并列返回
旧历史与新事件，但不得把旧行推测成新状态转换。

### 4. Adapter 迁移批次

1. **票据 12（依赖 06）**：建新表与 `AlarmRuntime`，先迁移实体观测；旧路径继续
   只读展示。状态机成功后才允许实体 Adapter 停止直写 `t_alarms`。
2. **票据 13（依赖 12）**：标签与 MQTT Adapter 只构造 `AlarmObservation`；来源
   topic、tag、故障码等放入 evidence，不改变统一接口。
3. **票据 14（依赖 10、13）**：规则 Adapter 只提交观测；删除四条旧写库函数、
   `POST /alarms` 测试旁路、`PUT /alarms/{id}/resolve` 与前端“恢复”按钮。

每批迁移必须保持主线绿色，并用静态检查证明该批调用方不再出现
`INSERT/UPDATE t_alarms`。最后为旧表撤销应用写权限，形成数据库级 contract gate。

### 5. 查询与告警中心

告警中心使用事件语义：

- 统计项为活动未确认、活动已确认、已恢复事件数，不显示采样累计次数。
- 活动卡片只有“确认”；确认后继续显示为活动，直到现场恢复。
- 事件详情展示定义版本、实体实例、触发证据、确认记录、恢复证据和完整转换时间线。
- API 提供事件列表、事件详情/时间线和确认命令；不提供人工恢复命令。
- operator 身份来自认证上下文，不再由前端固定传 `operator` 字符串。

## Test seam to confirm

在开始 TDD 前，请维护者确认以下两个公开测试缝：

1. **辅助状态机缝**：通过 `AlarmRuntime.submit()` 与
   `AlarmRuntime.acknowledge()`，使用固定时钟和持久化 Adapter，断言公开状态、
   转换、原因码、事件唯一性和 outbox；不测试私有函数或 SQL 字符串。
2. **主交付缝的告警切片**：通过公开解决方案包/API 安装一个告警定义，协议模拟器
   提交现场值，公开告警 API 完成查询与确认，最后由机器验收报告证明
   触发→确认→现场恢复；测试不直连数据库。

首轮辅助用例必须至少证明：

- 持续时间不足时不活动，达到时只创建一个活动事件；
- 高频重复活动观测不增加事件、不重复通知；
- 确认不恢复，且重复确认幂等；
- 只有恢复条件持续满足才恢复，边界抖动不会恢复；
- 同一观测经 entity/tag/MQTT Adapter 得到同一状态轨迹；
- 旧历史保持只读且不被转换为新事件。

不新增属性测试库；首版使用 pytest 参数化与固定时钟。若以后需要 Hypothesis，按
仓库规则另行申请依赖。

## Consequences

- 告警复杂度集中，四类来源共享一套生命周期、幂等和审计语义。
- 迁移需新增表和查询兼容层，不能只改告警中心页面。
- 票据 12 必须等待实体实例票据 06；否则多设备告警仍可能串对象。
- 删除人工恢复会改变现有操作习惯，但这是可信现场记录的必要约束。
- 当前 `alarm_count` 异常不会被猜测性修正；旧记录只读保留，新事件从切换点开始。
