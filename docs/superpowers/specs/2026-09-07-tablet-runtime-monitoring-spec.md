# 运行首页与设备监控交付规格

2026-09-07 · 待一次整体确认，尚不实施。首页属首批 A，独立设备卡片属后续；遵循[完整规格总索引](2026-09-07-tablet-full-delivery-design.md)的视觉、共用契约及任务边界。

## 实际依据

- 正式首页：`frontend/src/pages/EMSWorkbenchPage.tsx:149`，入口：`frontend/src/App.tsx:280`。后续新增 `DeviceMonitorPage.tsx`；Demo 布局参考 `frontend/demo-tablet/README.md:37`。
- `GET /api/v1/ems-workbench` 的 `WorkbenchEntity` 已有 `entity_instance_id/node_id/node_name/definition_id`，但异常响应只给 `unavailable/code`，不提供最后值或 freshness（`backend/app/services/ems_workbench.py:82`）。旧 KPI 按固定键取首项不能作为新首页绑定依据。
- 复用 `EntityInstance.freshness_seconds`、`EntityInstanceObservation`、逐实体 `realtime/history`、`/alarms/counts` 与提交帧快照/增量。依据：`frontend/src/api/client.ts:1251,1505,1865`、`frontend/src/api/committedFrameStream.ts:77`；后端观测含 `fresh/age_ms/quality/value_observed_at`（`backend/app/services/entity_instance_runtime.py:166`）。

## 展示与状态

1. 首页按真实节点展示 L2 指标，标题含节点、语义和单位；关联键唯一为 `entity_instance_id`，同时校验 `node_id/definition_id`。同定义多节点分别显示；只有明确站级 L2 才称汇总。不在前端求和、平均 SOC、猜测命名绑定或制造能流箭头；缺站级配置显示“未配置”。
2. 实时值仅消费已提交 L2。复用快照→游标增量→失效重取；按可见节点去重订阅，离页释放，旧请求不得覆盖新选择。值旁显示质量、时间及可查看的来源/帧证据；无帧证据明确缺失，不拼出虚假一致帧。快照后端已按实体时效计算有效质量；帧没有 `fresh/age_ms`，不能与另一份无帧序号的逐实体观测拼成一致帧。空闲期需要重新验证时由共享生命周期重取既有快照，不另造质量阈值；断线/读取失败不能保留“当前正常”。最后值仅在真实证据存在时展示并标明时间；无值为 `—`，真实零保留。
3. 历史按实体按需读取，采用真实时间戳；单位不混用，非 GOOD 点断线，禁止跨缺口连线。BOOL/ENUM/字符串原样展示状态历史，不绘制伪数值曲线。来源追溯可看 L0→L1→L2。
4. 后续设备页按已保存节点类型/类别筛选，未知归“其他”，不按名称猜身份；支持名称/ID搜索、仅有告警、每页六卡。无 L2 节点仍标未配置。详情同窗展示实体（10/20条分页）、历史、来源，共用同一实体选择。
5. 告警计数只算本节点未恢复事件，已确认未恢复仍计入；不冒充整个子树汇总，质量正常不代表无告警。复用无参 `fetchAlarmCounts()` 返回的全站节点映射，成功响应缺该节点才为零，失败显示未知（现有 SQL：`backend/app/api/alarms.py:131`）。不使用尚不兼容的客户端多ID逗号参数。三角色均可只读查看；仅工程师/管理员有“配置此节点”跳转，无新增控制、规则发布或通知操作。空站、无结果、未配置、无采样、超时、坏质量、请求失败分别提示，失败可重试。

## 可执行验收

- [ ] 隔离数据两节点同定义取不同值，交换响应顺序/快速切换后仍不串值；无站级实体不出现总功率或方向，零不变为空。
- [ ] 新提交无刷新可见；GOOD但超时、坏质量、断流、恢复分别验证，旧响应和旧游标不覆盖新数据。
- [ ] 两实体历史不混线；非 GOOD 中点形成断口；状态原值、无采样和来源缺失均如实显示。
- [ ] 三角色及1280×800/1024×768验证；后续验六卡分页、空节点、告警确认仍计数、计数失败；切页与重试无业务写请求。
- [ ] 执行 `python scripts/verify_delivery.py`、正式前端 `npm run build`、本模块模型/浏览器专项及节点主干；现场按总规格只读复验。节点写入测试只在明确授权的隔离根执行；缺证据为 INCOMPLETE，已执行失败为 FAILED，不复用 Demo 通过记录。

## 后续实施归属

模块拥有首页、设备页及专属 `runtime-monitoring/` model/hooks/测试。`App.tsx`、`api/client.ts`、共享提交帧契约归总集成任务；本模块仅建议补齐既有观测字段，等待共享契约就绪。无需新实体模型、仪表盘设计器、采集存储规则、依赖或现场假数据。剩余重大设计决策：无；待一次总确认。
