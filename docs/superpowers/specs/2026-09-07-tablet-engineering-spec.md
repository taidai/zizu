# 平板工程配置完整交付规格

日期：2026-09-07。状态：配合已确认的首批 A 迁正式，待[完整规格包](2026-09-07-tablet-full-delivery-design.md)一次书面确认；本文封闭全部工程页行为和并行边界。

## 目标与不变量

正式工程主线保持“真实节点树 → L0 原始点位 → L1 点位加工 → L2 全局实体”。只迁入 Demo 已确认的红金亮银布局、弹窗和十行密度，不迁入合成数据、内存保存、角色切换或模拟回读。沿用 ADR-0011：同节点 L1 可读本节点 L0；跨节点只能选择其他节点已标准化 L2，并经现有强类型公式预检及 DAG 阻断循环。L2 的 `entity_instance_id`、定义、单位、质量、时间和来源证据必须稳定，编辑不得换身份。数据库 Schema、配置格式、采集、JDM、告警和控制接口均不变。

## 页面行为

- 左侧树直接使用 `fetchNodes/createNode/updateNode/deleteNode`：支持搜索、展开、选择、新建根/子节点、改名/类型/父级/排序及退役。退役明确包含子树、保留历史；操作员只读。切换节点须取消旧请求并清除选择、草稿、预览，迟到响应不得覆盖当前节点。
- “导入点位”沿用 `fetchNeuronNodes/fetchNeuronGroups/previewNeuronTags/importNeuronTags`。弹窗先选 Neuron 节点和组，再逐项展示 create/update/unchanged/conflict、原因、数量与基础配置修订；有冲突禁止确认。确认必须携带预览返回的 `preview_digest` 和原 `selected_groups`，预览后改选择即失效，不提供绕过预览的写入口。
- L0 默认实时页：保留提交帧快照＋游标增量、链路/质量/时间、名称与类型筛选；另有历史页，按单一物理数值点及 1h/24h/7d 调用现有 `fetchTagHistory`，趋势与明细不混点、不补假值。维护弹窗复用 `maintainRawPoints/deleteRawPoints`，单点改显示名、批量启停；永久删除须二次确认并说明同时删除历史。停用点可维护但不能作为新加工的已选输入。
- “加工为实体”改为所选点位上下文中的弹窗，复用 `InlinePointProcessingPanel` 的直通、0/1 布尔、倍率偏移、状态映射、公式、控制声明和试算模型，提交仍走 `createPointProcessingDraftPlan`。新建默认产生独立草稿：先对当前L2目录查重；计划返回后再检查本次目标 `entity_definition_id` 的 `output_binding`，仅 `add` 可按新建继续，`preserve/update` 阻断并引导“编辑当前加工”（不阻断其他未改输出的保留项）。编辑必须明确既有实体，绝不按 key 静默覆盖。取消、换点、换节点后草稿失效。
- L1 生命周期复用 `DataTrunkWorkspace`、`PointProcessingTemplateManager`：安装模板先 `createPointProcessingPlan` 检查再应用；编辑当前加工只产生本节点新修订；模板“下一修订”“另存新模板”“从当前设备创建”均先 `validatePointProcessingTemplate`，再以 `importPointProcessingTemplate` 新增不可变版本。实施工程师可编辑/安装，只有管理员可创建共享模板；操作员只看运行数据。停用先生成依赖预览，存在上层依赖不得确认。
- L2 复用 `fetchNodeDataTrunk/fetchEntityInstances`、提交帧投影和 `fetchEntityInstanceHistory`，实时卡/表及展开详情显示实体身份、类型、单位、质量、时间、加工修订和 L0/L2 来源。跨节点选择器只列 L2，不暴露远端 L0。所有列表默认每页 10 条、可选 20 条，筛选或节点变化回第一页；1280×800 展示十行，1024×768可滚动，主要按钮和弹窗确认/取消触控高度至少 44px。

## 一致性、失败与文件边界

计划应用继续使用 `plan.id + digest + Idempotency-Key` 及 `dataTrunkRetryState`。网络错误、5xx或响应体无法读取时显示“结果未知”，保留同一计划和幂等键供重试/刷新恢复；不得显示失败、成功或生成新请求替代。明确业务拒绝才清除恢复记录。节点/Neuron/维护的失败保持原对象和用户输入，不乐观改本地真相。

未来“工程配置”任务独占 `NodeTreePage.tsx`、`NodeTagPanel.tsx`、`RawPointHistoryPanel.tsx`、`components/node/*`、`components/data-trunk/*` 及对应前端测试；共享的 `committedFrameProjection.ts` 及其测试除外，归总协调。工程任务不改告警、调度、控制、工作台实现。`App.tsx`、`api/client.ts`、`index.css`/全局主题由总协调任务独占：本任务只要求其保留三角色传参与正式导航、暴露上述既有函数和类型、提供红金亮银 token 与 44px 触控基线；不自行新增端点或修改类型契约。

## 回归验收矩阵

| 面向 | 必验结果 |
|---|---|
| 树/权限 | 管理员、工程师完成增改退役；操作员无写入口；换节点无串态 |
| Neuron/L0 | 预览摘要与逐项一致，冲突/过期摘要阻断；实时断流恢复；历史空态；名称、启停、删除确认真实生效 |
| L1/L2 | 新建不覆盖；模板新版本不改旧版本；检查、试算、安装、编辑、依赖阻断、同幂等键未知结果恢复；L2 身份/历史/来源稳定，跨节点无 L0 |
| 平板/回归 | 10/20 分页、1280×800十行、1024×768滚动、44px触控；登录/退出、首页、告警、完整JDM调度及统一控制原流程不回退；正式 `npm run build` 与既有前端/后端门禁通过 |

所有写验收只在隔离测试根和测试数据执行，不连接或读写现场设备，不部署、不通知。任一主链或角色未验证为 INCOMPLETE，断言失败为 FAILED。

## 已查实的最小缺口

`tags.py:list_tags` 已支持每页1–200条，10/20只需改前端固定50条；Neuron重新扫描后摘要不符已返回409 `NEURON_IMPORT_PREVIEW_STALE`，配置并发失效为409 `CONFIGURATION_REVISION_STALE`，无需新契约。现有 `point_processing.py:preview_node_definition` 按相同业务定义合并并复用L2，不具有独立的服务端create语义；因此本次实现上述UI目录/计划双检，并保留已有应用计划的配置修订栅栏，防止过期预览覆盖并发新增。验收必须覆盖旧目录、新计划返回preserve、计划后配置变化三种情况。不得宣称服务端所有调用已变为create-only，也不为此次UI改版新增create/edit协议或Schema。
