# 平板告警、调度、控制与系统工具交付规格

日期：2026-09-07。状态：红金亮银与首批 A 已确认；本文属[完整交付规格包](2026-09-07-tablet-full-delivery-design.md)，待一次书面确认，**不表示这些能力已经开发、测试或部署**。

## 1. 范围与共同边界

首批只迁入红金亮银样式、导航分组和触控密度；`AlarmCenterPage`、`DispatchStrategyPage`、`AdminPanel` 及控制流程保留现有正式业务语义，只统一视觉。Demo 的合成事件、内存草稿、角色切换、故障注入、模拟通知和模拟回读一律不进入正式构建。后续才交付本文定义的新编辑体验；不新增依赖、数据库模型、任意 HTTP 动作、第二规则引擎、Cron、优化器或新的安全阈值。

共享入口、路由、认证与主题由主任务负责：`frontend/src/App.tsx`、`frontend/src/api/client.ts` 和全局 theme 不属于任何模块任务。模块只能提出接口/主题契约，不能并行修改这些文件。

## 2. 告警与 HTTP 通知

正式告警页由固定50条改为默认10条表格（可选20条），保留未恢复、已确认、已恢复、已归档、全部（未归档）筛选、实体与等级筛选及5秒刷新。行点击打开事件详情弹窗，展示事件身份、状态、节点/L2、发生与恢复依据、确认记录和 transitions；不得用同节点实体或当前值伪造触发证据。表头“选择当前页”只选择本页仍可确认的事件，翻页、切页签或改变筛选即清空选择；确认仍逐条调用既有 `acknowledgeAlarm`，部分失败必须逐项报告并刷新，不能暗示整批成功。事件只有已恢复后归档，保留证据；不新增事件物理删除。

数据继续来自 `fetchAlarms`、`fetchAlarmEntities`、`acknowledgeAlarm`、`archiveAlarm` 及现有 `/alarm-events/{id}`、`/transitions` 契约。规则页继续复用 `MinimalAlarmRulesPage` 的试算、计划/apply、启停和 HTTP 配置绑定。通知页复用 `AlarmNotificationRecords` 的真实投递、重试及单条/当前页批量永久删除：仅管理员/工程师可删除 delivered/failed/cancelled，pending/retry_wait不可删；投递尝试记录随任务一并物理删除。系统工具中的 `AlarmHttpNotificationPanel` 继续使用真实配置、测试、启停接口。HTTP 投递失败只改变通知任务，不改变告警状态；禁止加入 Demo“发送成功”。

## 3. 调度策略、JDM 与控制

保留正式导航权限：管理员/工程师进入调度页，先看已启用修订、健康、最近评估、阻断/失败原因及控制回读，再进入配置；操作员不新增调度页入口。编辑顺序为：选择一个或多个已确认 L2 输入并定义唯一别名 → 在原生 JDM 表中编辑规则 → 把输出别名绑定到已确认且可控的 L2。保存必须提交完整 `jdm_content`、完整有序 `bindings`、`expected_digest` 与 `base_configuration_revision`，草稿持久化到服务端，刷新或离页后可恢复。

现有 `DecisionGraph` 是事实来源。结构为明确且支持无损往返的单表时，原生 `DecisionTable` 只编辑该 decision-table 节点，再无损写回原 graph。模型不是单表、存在多个候选表或 round-trip 不一致时，继续使用现有完整图编辑器、禁用简化单表保存；未知节点、边、表达式、元数据和其他表均须保留。无需新增选表流程或执行模式，绝不静默转换或用 Demo 单表覆盖完整 graph。

试算调用现有 `simulateDispatchStrategy`，只返回快照、命中、决策和拟产生意图；保存、试算、发布、启用是四个独立动作。发布继续冻结不可变修订并复核摘要/配置修订，启用继续取得输出所有权；失败、阻断、未知结果显示稳定原因，不自动重试、清锁、降级或改写活动修订。运行只消费 committed L2，经现有策略 runtime 产生控制意图，再走统一控制确认、幂等、安全门、命令状态与写后 `readback`；HTTP 受理、JDM 命中或前端刷新都不能模拟设备成功。

## 4. 系统工具与文件边界

系统工具仅重排现有 `AdminPanel`：pipeline、MQTT、SQL/清表及 HTTP 通知仍调用原接口、沿用原权限和危险操作确认；不复制 Demo 工具卡，也不扩大操作员权限。

告警、调度和系统工具归同一A任务：拥有 `AlarmCenterPage.tsx`、`MinimalAlarmRulesPage.tsx`、`components/alarm-center/`、`DispatchStrategyPage.tsx`、`components/dispatch-strategy/`、`AdminPanel.tsx` 和 `components/admin/` 及专项测试。没有独立控制任务；`EMSWorkbenchPage.tsx` 及其中控制区域由R任务拥有。共享文件变更以契约清单交给总协调串行处理，模块间不得互改。

## 5. 两阶段完成标准与已知缺口

首批 A 完成：正式导航和各页在1280×800、1024×768下统一红金亮银，列表可见十条、原弹窗与44px主操作可达；原告警、完整 graph、草稿/试算/发布/启停、通知和控制回归无损。此阶段保留原分页容量，不要求新的十条分页表/详情弹窗/JDM体验已实现。

后续体验完成：上述10条告警表与当前页选择、真实详情/通知、L2→原生表→控制绑定、服务端草稿恢复、四阶段生命周期和失败态全部通过隔离正式前后端验收；统一控制用既有隔离写入/回读边界验证，不连接真实设备。Demo检查不能替代。已查实保存/发布服务端可能返回 `STRATEGY_DRAFT_STALE`／`DATA_FRAME_CONFIGURATION_STALE`，前端专用中文映射尚不完整；须补齐冲突提示并使旧试算/预览失效，不把拒绝当成功。无剩余重大设计取舍。部署由总规格的D0负责，本模块不单独连接现场、发送通知或写设备。
