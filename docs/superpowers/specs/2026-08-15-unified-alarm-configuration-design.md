# 统一告警配置与告警验收设计

**状态：** 已确认  
**日期：** 2026-08-15  
**适用目标：** 将告警等级融合到告警配置中，并通过公开产品链路验收告警功能

## 1. 背景

当前平台同时保留三套告警配置语义：

1. `t_tags.alarm_level` 使用 `error1/error2/error3` 的旧点位配置；
2. `t_alarm_levels` 与 `t_entity_alarm_bindings` 提供旧实体等级和触发规则；
3. 统一告警状态机使用版本化 `alarm_definition`，严重度为
   `CRITICAL/MAJOR/WARNING/INFO`。

前端因此存在独立的“告警等级”和“告警配置”页面，但新告警事件实际上由第三套模型运行。
仅合并菜单会继续保留语义分叉，无法形成配置型工业 IoT 平台的可信交付路径。

## 2. 目标与非目标

### 2.1 目标

- 只保留一个“告警配置”工作区；告警等级成为每条配置的内在属性。
- 支持批量选择实体实例和批量定义告警规则。
- 批量操作在应用前提供确定性的展开预览、冲突与阻断。
- 运行期只使用版本化 `alarm_definition` 和统一 `AlarmRuntime`。
- 旧点位等级、旧实体等级和绑定经人工确认迁移，迁移后只读。
- 通过公开产品界面、公开 API、协议模拟器和持久化数据库证明告警完整生命周期。

### 2.2 非目标

- 不新增可编程脚本或通用规则语言。
- 不让告警配置直接引用节点、MQTT topic、Neuron 地址或原始 tag。
- 不允许人工恢复告警；恢复只能由现场观测证明。
- 不把批量规则组作为运行时共享的可变模板。
- 不猜测性改写旧告警历史。

## 3. 核心领域决策

### 3.1 固定严重度语义

严重度固定为：

- `CRITICAL`
- `MAJOR`
- `WARNING`
- `INFO`

站点可以配置中文显示名和颜色，但不能增加新的严重度枚举或改变四级语义。统计、通知、
权限和验收均使用稳定枚举。

### 3.2 规则组只用于编排

`AlarmRuleSet` 是可复用、版本化的实施对象。一个规则组包含多条规则，每条规则至少声明：

- 稳定规则 ID；
- 名称与严重度；
- 触发运算符、阈值和持续时间；
- 恢复运算符、阈值和持续时间；
- 通知节流；
- 可选故障码映射引用。

规则组更新产生新 revision。运行时不动态引用规则组；应用配置时把规则组展开为独立的
`alarm_definition`，避免修改一个共享模板后静默改变所有设备。

### 3.3 批量展开

实施工程师可以通过以下范围批量选择实体：

- 手工多选实体实例；
- 按设备实例分组选择；
- 按实体定义选择全部匹配实例。

仅已确认绑定、类型和单位相容的实体实例可进入计划。若选择 4 个实体和 3 条规则，计划必须
明确显示将生成 12 个独立定义。每个定义拥有稳定 ID、独立版本、独立状态和独立审计。

默认限制为单次最多 200 个实体、20 条规则，展开后最多 2,000 个定义。超过限制返回稳定
阻断，不做部分应用。

## 4. 模块边界

新增深模块 `AlarmConfiguration`，公开操作保持最小：

```python
class AlarmConfiguration:
    def plan(self, command: PlanAlarmConfiguration) -> AlarmConfigurationPlan: ...
    def apply(self, command: ApplyAlarmConfigurationPlan) -> AppliedAlarmConfiguration: ...
    def current(self, query: AlarmConfigurationQuery) -> AlarmConfigurationView: ...
    def preview_legacy_migration(self) -> LegacyAlarmMigrationPlan: ...
```

模块负责选择范围解析、规则组 revision、展开、校验、计划摘要、站点配置并发检查、原子应用和
审计。FastAPI 路由只解析请求并映射稳定错误；前端不复制领域判断。

现有 `AlarmRuntime.submit/acknowledge` 接口保持不变。配置模块只生成和切换定义版本，不能创建、
确认或恢复事件。

## 5. 持久化与版本

需要持久化以下概念：

- 规则组及其不可变 revision；
- 配置计划、基准站点配置版本、计划摘要、展开项、冲突和阻断；
- 每个定义的来源：解决方案包、规则组 revision 或站点覆盖；
- 当前定义指针和不可变历史版本；
- 旧配置迁移候选、解析结果、目标定义和确认主体。

应用计划必须在一个事务中完成：

1. 锁定单站配置；
2. 复核计划摘要、当前站点配置版本和规则组 revision；
3. 以当前解决方案包和安装为基准创建派生站点配置安装，复制未变化的参数、Secret 引用、
   实体实例和其他配置资产；
4. 写入独立告警定义版本；
5. 更新当前定义指针、站点配置版本和派生安装记录；
6. 追加配置审计；
7. 标记迁移候选状态（若适用）。

告警配置应用不能在站点版本链之外单独改表。派生安装继续引用同一个不可变解决方案包摘要，
因此发布锁、回滚和交付报告仍能锁定完整站点状态。任一步失败则整批回滚。已处于 pending 或
active 的事件继续使用其原定义版本；新事件才使用当前版本，避免升级途中改变既有事件语义。

## 6. 公开 API

公开面固定为：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/alarm-configurations` | 查询当前配置、来源、版本和待迁移数量 |
| GET | `/api/v1/alarm-rule-sets` | 查询规则组及 revision |
| POST | `/api/v1/alarm-rule-sets` | 创建规则组首个 revision |
| POST | `/api/v1/alarm-rule-sets/{id}/revisions` | 创建不可变新 revision |
| POST | `/api/v1/alarm-configuration-plans` | 解析范围并生成展开预览 |
| GET | `/api/v1/alarm-configuration-plans/{id}` | 查询计划、冲突和阻断 |
| POST | `/api/v1/alarm-configuration-plans/{id}/apply` | 使用 `Idempotency-Key` 原子应用 |
| GET | `/api/v1/alarm-configuration-migrations/legacy` | 查询旧配置迁移候选 |
| POST | `/api/v1/alarm-configuration-migrations/legacy/plans` | 生成受控迁移计划 |
| POST | `/api/v1/alarm-configuration-applications/{id}/acceptance` | 汇总新建/更新定义的生命周期证据 |
| GET | `/api/v1/alarm-configuration-reports/{id}` | 读取不可变告警配置验收报告 |

修改、停用和删除同样通过计划表达，不提供直接修改当前定义的旁路。

admin 和 engineer 拥有配置读写能力；operator 不读取配置细节，只通过告警中心读取和确认事件。

## 7. 统一告警配置工作区

前端删除独立“告警等级”导航，只保留“告警配置”。工作区包含：

1. **配置列表：** 名称、实体实例、严重度、触发/恢复摘要、来源、版本、启用状态；
2. **批量编排：** 选择实体范围、选择或编辑规则组、展开预览、解决阻断、确认应用；
3. **规则组：** 管理版本化规则集合和中文显示；
4. **待迁移：** 展示旧配置、解析结果、目标定义和阻断原因；
5. **变更详情：** 新增、更新、保留站点覆盖、删除候选、冲突与审计主体。

产品界面不要求用户填写 UUID、JSON、SQL、节点地址或 tag 路径。

## 8. 校验与稳定错误

以下情况在计划或应用阶段阻断并保持零写入：

- 实体没有确认绑定或存在多个数据源：`ALARM_ENTITY_UNRESOLVED`；
- 数据类型不支持：`ALARM_DATA_TYPE_UNSUPPORTED`；
- 单位不兼容：`ALARM_UNIT_MISMATCH`；
- 规则 ID、定义 ID 或选择项重复：`ALARM_RULE_CONFLICT`；
- 触发/恢复阈值关系不安全：`ALARM_THRESHOLD_INVALID`；
- 批量展开超过上限：`ALARM_BATCH_LIMIT_EXCEEDED`；
- 计划摘要不匹配：`ALARM_PLAN_DIGEST_MISMATCH`；
- 站点配置或规则组 revision 已变化：`ALARM_PLAN_STALE`；
- 旧配置无法唯一映射：`ALARM_MIGRATION_AMBIGUOUS`；
- 旧配置已全部迁移、没有待迁移定义：`ALARM_MIGRATION_NOTHING_TO_MIGRATE`（HTTP 409，零计划、零运行态写入）；
- 幂等键用于不同请求：`IDEMPOTENCY_KEY_REUSED`；
- 审计无法持久化：`AUDIT_UNAVAILABLE`。

所有错误使用既有 `{"detail":{"code","message"}}` 包络。请求正文、令牌和现场敏感值不进入日志或
审计。

## 9. 旧配置迁移

旧配置只进入迁移清单，不再作为新运行配置：

- `error1 → CRITICAL`；
- `error2 → MAJOR`；
- `error3 → WARNING`；
- 自定义旧等级按已有 `severity` 映射；
- 点位配置必须通过确认绑定唯一解析到实体实例；
- 旧实体定义若对应多个设备实例，必须由实施工程师显式选择范围；
- 故障码映射只有在引用仍有效时才迁移。

迁移成功后旧数据保持只读并记录目标定义，不删除旧历史，也不重新参与 pipeline。回滚只能切换到
已验证的新定义版本，不能重新启用旧写路径，以免产生双重告警。

兼容窗口内旧 GET 接口可继续读取；旧创建、更新、绑定和 tag 告警字段写入返回
`ALARM_CONFIGURATION_MIGRATION_REQUIRED`。窗口结束后删除旧写 API，并由静态和数据库权限门禁证明
不存在旧运行写入。

## 10. 告警验收

### 10.1 辅助测试

- 规则组 revision 和稳定规则 ID；
- 范围解析和实体×规则确定性展开；
- 单位、类型、阈值、重复和批量上限；
- 计划摘要、并发 stale 和幂等键冲突；
- 原子应用、审计失败回滚和旧配置迁移分类。

### 10.2 公开 HTTP 测试

- admin/engineer 允许配置，operator 返回稳定 403；
- 所有阻断返回稳定机器码并证明零写入；
- 同计划同幂等键返回同一结果；不同请求复用键被拒绝；
- 前端所需操作全部可由公开 API 完成。

### 10.3 PostgreSQL 持久化测试

- 真实迁移、计划应用、站点配置版本和审计同事务；
- 并发应用恰好一个成功；
- 进程重启后规则组、当前定义、事件、时间线和审计不变；
- 应用角色不能写旧 `t_alarms` 或旧配置运行路径。

### 10.4 公开告警主缝

仅通过产品/API 安装批量告警配置，协议模拟器发布多个实体观测，并证明：

1. 正常值不产生告警；
2. 超阈值但持续时间不足只进入 pending；
3. 达到持续时间后每个定义只生成一个活动事件；
4. WARNING、MAJOR、CRITICAL 等级统计准确；
5. 高频重复观测不重复创建事件或通知；
6. operator 确认后事件仍保持活动；
7. 坏质量或数据空洞打断触发和恢复连续性；
8. 只有恢复条件持续满足才进入 recovered；
9. 重启后事件详情、完整转换时间线和审计仍可读取；
10. 旧配置表和旧告警表没有新增运行写入。

最终机器验收报告必须引用本次安装的定义版本、事件 ID、确认审计和恢复证据，不能只检查页面或
HTTP 200。验收接口只观察通过正常运行链路产生的事件，不自行写模拟值、不确认事件，也不改变
状态。每个新建或更新定义都必须通过；preserve 项可以引用同一不可变定义版本的既有通过报告。

## 11. 完成定义

以下条件全部成立才算完成：

- 独立“告警等级”菜单消失；
- 等级、规则、对象和迁移都在统一“告警配置”工作区完成；
- 批量实体与批量规则可预览并原子应用；
- 运行时只消费版本化 `alarm_definition`；
- 旧配置不再新增或参与运行；
- 相关测试、完整后端套件和前端构建通过；
- 真实 PostgreSQL 与协议模拟公开主缝通过；
- 告警验收报告证明触发、确认、恢复、等级统计、审计和重启持久性。
