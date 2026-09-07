# 自足IOT 正式平板界面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已确认红金亮银设计接入正式首页、工程主干及现有应用，并统一验证发布。

**Architecture:** 正式App、API和提交帧保持同一入口；三个独占模块并行，公共导航/主题及发布由总协调完成。R1/E/A1构成首批，R2/A2分别形成完整提交，之后合入，不重写后端执行或数据库。

**Tech Stack:** 现有React18、TypeScript、Tailwind3、原生GoRules JDM编辑器、Playwright、FastAPI和PostgreSQL；不新增依赖。

**Spec:** `docs/superpowers/specs/2026-09-07-tablet-full-delivery-design.md` 及其链接的四份专项规格；整包已由用户2026-09-07明确确认。

## Global Constraints

- 自足IOT／光储充现场；基础红 `#bb0814`、暖金 `#b88a34`，亮银 `#fff → #f0f2f4 → #e0e4e8`，品牌金 `#f3d88b`；正常/故障语义色独立；Noto Sans SC及既有中文回退，不新增字体下载。
- 1280×800容纳十行，1024×768可滚动；主要触控按钮至少44px；不填假数据。
- 真实节点→L0→L1→L2；跨节点只读L2；上层只读committed L2。禁止把Demo内存保存、角色切换、模拟通知/回读接入正式入口。
- 既有角色、摘要/配置修订/幂等、安全门、统一JDM/控制、Schema不变；接口成功不是设备成功。
- R拥有EMSWorkbench/DeviceMonitor及runtime-monitoring；E拥有节点/点位/data-trunk（共享投影除外）；A拥有告警/策略/工具。D0独占App、API、共享帧文件、主题、版本、全局测试配置和发布。
- 模块只在自己的工作树修改与提交，不推送、不部署、不接现场、不发送通知、不写设备；公共文件请求交D0。只跑无外部副作用的模型/组件专项，正式数据库写验收由D0在隔离环境执行。
- 使用apply_patch编辑，保留用户已有dirty。每次行为变更先给出RED、实现后GREEN；独立任务报告同时给出规格符合性、自查与实际测试证据。审查与最终验收不由实现者自评替代。

## 并行与接口基线

任务2→5为R路，任务3为E路，任务4→6为A路；用户已明确选择并行，使用不同工作树与独占文件，覆盖技能的默认串行建议。任务1冻结本节接口后即可同三路并行完成壳层；任务7/8仅由D0执行。

视觉参考为 `docs/design/tablet-approved-reference.md`。模块使用 `neu-card/neu-inset/neu-input/neu-btn`，新主按钮 `zizu-primary`、选中标签 `zizu-tab-active`、危险按钮保持语义红。模块可创建仅本页CSS，不能全局覆盖质量色。

可选属性只增不破坏旧调用：

```ts
// EMSWorkbenchPage / DeviceMonitorPage exports stay default.
type RuntimeTab = 'overview' | 'trends' | 'alarms' | 'controls'
type RuntimeProps = {
  onOpenAlarms: () => void
  onOpenEngineering?: (nodeId?: string) => void
  onOpenDevices?: () => void
  initialTab?: RuntimeTab
}
type DeviceMonitorProps = { onOpenEngineering?: (nodeId: string) => void }
// NodeTreePage keeps all existing props and adds:
type NodeNavigationProps = { initialNodeId?: string }
```

实时仍用 `fetchCommittedFrameSnapshot(nodeId, signal)`、`connectCommittedFrameStream({nodeId,cursor,onDelta,onResnapshotRequired,onError})`、`replaceSnapshot/applyFrameDelta`；返回类型在 `api/committedFrameStream.ts`。目录为 `fetchEntityInstances()`，告警计数为无参 `fetchAlarmCounts()`；历史为 `fetchEntityInstanceHistory`。模块不得改这些函数签名。

### Task 1: D0 — 正式应用外壳与主题

**Files:** 修改 `frontend/src/App.tsx`、`frontend/src/index.css`、`frontend/index.html`；新增 `frontend/src/appNavigationModel.ts`、同名`.test.mjs`及 `frontend/e2e/tablet-shell.spec.ts`。仅需要时补 `frontend/src/api/client.ts` 既有返回字段类型。

**Interfaces:** 消费上面可选Props；提供同一会话下运行/工程导航和 `zizu-primary/zizu-tab-active`，不新增后端调用语义。

- [ ] RED：新增导航模型测试，操作员请求策略/工具/工程区均回运行首页，工程师不进入系统工具；运行区允许告警和原授权控制。初始不存在模型时先加入函数签名，再以错误返回观察断言失败。

```ts
export type TabletPage = 'workbench' | 'monitor' | 'tree' | 'alarms' | 'strategies' | 'admin' | 'controls'
export function resolveTabletPage(role: 'admin'|'engineer'|'operator', page: TabletPage): TabletPage {
  if (page === 'admin' && role !== 'admin') return 'workbench'
  if ((page === 'tree' || page === 'strategies') && role === 'operator') return 'workbench'
  return page
}
```

- [ ] GREEN：App保留认证恢复/退出/失效订阅，外壳改红色页头、工程入口及固定日常导航；`monitor`首批渲染readOnly树，后续接DeviceMonitor；`controls`传`initialTab='controls'`，不自动调用任何控制API。健康获取失败标“连接未知”，不保留绿色旧健康状态。用正式页面与真实角色响应做无头壳测试，断言导航/弹窗可达、无越权写请求；先看到旧壳断言失败，再替换布局和主题。

```css
:root { --zizu-red:#bb0814; --zizu-gold:#b88a34; --zizu-silver:linear-gradient(135deg,#fff,#f0f2f4 35%,#e0e4e8); }
.zizu-primary { background:var(--zizu-red); color:white; }
.zizu-tab-active { background:#eee4ce; color:#981320; border-color:#d5ba85; }
```

- [ ] 运行 `node --test --experimental-strip-types src/appNavigationModel.test.mjs`、正式`npm.cmd run build`及壳专项；记录结果后只提交本任务文件。

### Task 2: R1 — 真实运行首页

**Files:** 修改 `frontend/src/pages/EMSWorkbenchPage.tsx`；新增 `frontend/src/components/runtime-monitoring/runtimeModel.ts`、`.test.mjs`、`useRuntimeNodes.ts`、`runtime-monitoring.css`、`EntityRuntimeDetail.tsx`及 `frontend/e2e/tablet-runtime.spec.ts`。

**Interfaces:** 按 `RuntimeProps` 保留旧onOpenAlarms；使用目录与节点级提交帧。Controls保持既有确认/提交/查询/回读函数，不能增加隐式调用。所属规格为 `2026-09-07-tablet-runtime-monitoring-spec.md`，完整公共约束和视觉参考必读。

- [ ] RED：`runtimeModel.test.mjs`用两节点同definition不同entity id的真实完整帧fixture，断言只按id/node关联；零保留、非GOOD带最后时间、断流不显示当前正常；单位不同不混线；历史BAD中点形成断口。实现此可测入口并从真实页面调用：

```ts
export function numericHistorySegments(points: Array<{observed_at:string;value:unknown;quality:number}>): Array<Array<{time:number;value:number}>> {
  const result: Array<Array<{time:number;value:number}>> = []
  let segment: Array<{time:number;value:number}> = []
  for (const point of points) {
    const time = Date.parse(point.observed_at)
    if (point.quality !== 192 || typeof point.value !== 'number' || !Number.isFinite(point.value) || !Number.isFinite(time)) {
      if (segment.length) result.push(segment)
      segment = []
    } else segment.push({time,value:point.value})
  }
  if (segment.length) result.push(segment)
  return result
}
```

- [ ] GREEN：首页按真实节点/L2身份生成指标，拒绝旧工作台首项KPI与前端汇总；未配置显示说明。每可见节点快照→增量→失效重取，清理请求/订阅，generation拒旧响应，空闲到期共用节点快照重验，不另造质量阈值。详情按单实体查询历史/证据；BOOL/字符串状态原样显示。真实目录或计数失败分别可重试，不能归零。onOpenEngineering仅在父层给定时展示。
- [ ] 替换页内旧混合趋势，Controls业务保留，仅材质和表单布局升级；写React组件时先在本页无头专项验证两节点交换返回、不串值、零与陈旧值、导航不提交，再运行模型测试和build。
- [ ] `node --test --experimental-strip-types src/components/runtime-monitoring/runtimeModel.test.mjs`与build通过后提交R1；报告提交SHA、RED/GREEN和共享契约请求。R2另一个提交，不混合。

### Task 3: E — 节点、点位、加工、实体

**Files:** 修改 `frontend/src/pages/NodeTreePage.tsx`、`components/NodeTagPanel.tsx`、`components/RawPointHistoryPanel.tsx`、`components/node/nodeUsabilityModel.ts`及测试、`components/data-trunk/InlinePointProcessingPanel.tsx`、`inlinePointProcessingModel.ts`及测试、`DataTrunkWorkspace.tsx`、`EntityDataPanel.tsx`、`PointProcessingTemplateManager.tsx`；可新增本模块CSS与 `frontend/e2e/tablet-engineering.spec.ts`。共享`committedFrameProjection.ts`及其测试禁止修改。

**Interfaces:** 保留NodeTreePage旧props，增加可选initialNodeId；只在目录已返回且目标真实存在时选中。复用既有preview/import/plan/apply/maintenance API，新增目录查重与计划查重不改后端create/edit语义。读取 `2026-09-07-tablet-engineering-spec.md` 和公共约束/视觉参考。

- [ ] RED：追加新建撞key、旧目录收到preserve计划、新建add成功、其他保留输出不误阻断四测试；节点切换清草稿/选择，10/20分页重置和逐项导入预览另加受影响模型/浏览器测试。

```ts
export function isNewOutputPlan(plan: {items:Array<{kind:string;entity_definition_id?:string;action:string}>}, definitionId:string): boolean {
  const target = plan.items.filter(item => item.kind === 'output_binding' && item.entity_definition_id === definitionId)
  return target.length === 1 && target[0].action === 'add'
}
```

- [ ] GREEN：NodeTagPanel用`pageSize=10`状态传原`fetchTags`，可选20、筛选/节点变化回第一页；表单移入当前对象弹窗，保留输入/预览/摘要与同幂等恢复；新建只允许上述目标add，编辑明确当前实体。导入展示已有`preview.items`及冲突，修改组失效；409要求重查不自动应用。节点CRUD/子树退役、点位启停/永久删的既有依赖与确认不削弱。
- [ ] 更新加工/模板/实体详情布局为正式红金亮银与44px；只移动现有能力，管理员创建共享模板、工程师检查安装；跨节点选择器仍只有L2；实时与历史/来源和已保存数据不能因弹窗丢失。弹窗打开焦点进入、Escape/取消不写入、关闭回原触发控件；未保存离开提示只针对确有修改。
- [ ] 在独立工作树运行该目录模型测试和build；浏览器新用例不连接现场。报告真实主干隔离写验收尚须D0执行，提交只包含E文件。

### Task 4: A1 — 现有告警、策略、工具样式与回归

**Files:** 修改 `frontend/src/pages/AlarmCenterPage.tsx`、`MinimalAlarmRulesPage.tsx`、`DispatchStrategyPage.tsx`、`components/AdminPanel.tsx`、`components/admin/AlarmHttpNotificationPanel.tsx`、本模块专用CSS；必要的 `components/dispatch-strategy/dispatchStrategyModel.mjs` 与测试。无App/API/Controls代码所有权。

**Interfaces:** Props原样保留，所有保存/试算/发布/启用/控制函数签名不变。阅读 `2026-09-07-tablet-alarm-strategy-tools-spec.md`、公共约束及视觉参考。

- [ ] RED：补真实服务端`STRATEGY_DRAFT_STALE`与`DATA_FRAME_CONFIGURATION_STALE`错误呈现用例；原有草稿/完整图往返、告警trial、通知删除测试先保留基线。用隔离浏览器场景证明新44px操作/菜单不遮挡旧页面，不能用grep替代行为测试。
- [ ] GREEN：仅更换本页品牌按钮/选中态/留白与触控密度，质量/危险色不替换；完整JDM图仍用原生正式组件，未改页不能产生draft/apply请求。稳定拒绝显示重新加载/试算动作，旧结果失效，不自动覆盖草稿。

```js
const staleDraftCodes = new Set(['STRATEGY_DRAFT_STALE','DATA_FRAME_CONFIGURATION_STALE'])
// Feed the existing error formatter and preview invalidation branch.
const requiresReload = (code) => staleDraftCodes.has(code)
```

- [ ] 模型专项及build通过提交A1；报告不声称新的告警十条表/JDM编辑已经完成。随后执行Task6，单独提交。

### Task 5: R2 — 独立设备监控

**Files:** 新增 `frontend/src/pages/DeviceMonitorPage.tsx`；复用/完善R1专属runtime-monitoring组件和同一测试文件，新增 `frontend/e2e/tablet-devices.spec.ts`。

**Interfaces:** `DeviceMonitorProps`；`fetchNodes()`真实节点按已保存类别组织，目录按node_id归属；无参`fetchAlarmCounts()`取本节点未恢复数。R1提交后开始，不改App，由D0接路由。

- [ ] RED：六卡一页、两页切换清已选详情；同名不同id不串设备，未配置L2仍显示节点；已确认未恢复计数仍保留；计数请求失败不是0；设备详情10/20条、状态历史不转数值。

```ts
const pageItems = filteredNodes.slice((page - 1) * 6, page * 6)
const activeNodeIds = pageItems.map(node => node.id)
```

- [ ] GREEN：用真实目录和R1节点订阅，在可见六卡/详情范围内去重，翻页/离页释放；名称/ID搜索、类别/有告警筛选；缺计数时禁止伪造无告警筛选结果。详情展示实体/历史/来源，工程跳转仅父层传入时可见，页面本身无任何写入口。
- [ ] 模型、组件专项、build；提交R2，保留R1边界。最终路由接线与现场复验交D0。

### Task 6: A2 — 告警表格与通用原生JDM编辑

**Files:** `pages/AlarmCenterPage.tsx`、`pages/DispatchStrategyPage.tsx`、`components/alarm-center/`、`components/dispatch-strategy/`；新增 `nativeDecisionTableModel.ts`与`.test.mjs`、`NativeDecisionTableEditor.tsx`、本模块CSS、`frontend/e2e/tablet-applications.spec.ts`。原生编辑器已在package.json，无新增依赖。

**Interfaces:** 完整 `jdm_content`、有序 `bindings`、`expected_digest`和配置修订沿用原API。单表adapter只替换被验证唯一decision-table节点的content：

```ts
export function replaceDecisionTableContent(graph: {nodes:Array<{id:string;type:string;content?:unknown}>;[key:string]:unknown}, nodeId:string, content:unknown) {
  const candidates = graph.nodes.filter(node => node.type === 'decisionTableNode')
  if (candidates.length !== 1 || candidates[0].id !== nodeId) throw new Error('请使用完整规则图编辑此策略')
  return {...graph, nodes:graph.nodes.map(node => node.id === nodeId ? {...node,content} : node)}
}
```

- [ ] RED：未知节点/边/元数据与额外绑定round-trip保留，多表/不支持图走正式完整图不保存为单表；bool/numeric/string L2别名唯一、可控输出过滤、不固定SOC/时段；试算结果在任一输入/model变化后失效；刷新服务端草稿保留。
- [ ] GREEN：以现有JDM为唯一对象，界面三步“选全局实体→原生决策表→指定控制”，支持一个或多个有类型的输入和输出；保留完整图入口及全部原运行生命周期。不兼容内置2充2放的图继续完整图，不使用简表解析器覆盖。新增/删绑定保留用户未编辑字段，服务端语义/单位/所有权门禁照旧。
- [ ] 告警事件改10/20表、当前页复选框/确认，详情弹窗真实取事件与transitions；翻页/筛选清选中，部分确认失败逐项报告。事件只归档。通知记录保持真实终态永久删除、当前页范围，禁止删除待投递项；取消表单不产生请求。先写对应选择/分页/详情浏览器断言再实现。
- [ ] 运行新增model测试和原告警/调度全模型回归、build；提交A2；提供隔离FastAPI+DB+JDM联验需D0完成的用例清单，不私接现场。

### Task 7: D0 — 集成、正式联验与交付证据

**Files:** 必要的App接线、`frontend/e2e/tablet-*.spec.ts`整合、既有e2e导航适配；`docs/reviews/2026-09-07-tablet-production-acceptance.md`。

- [ ] 收任务提交与RED/GREEN证据，各自生成diff包，由独立审查检查规格符合性和质量；发现问题由原任务修复并定向复验。无依赖的已通过提交可串行合入；公共文件不能由模块暗改。
- [ ] 在隔离本机正式前后端配置数据跑节点→导入预览→L0实时/历史→加工预览/发布→L2身份/历史/来源→告警可选；调度用隔离控制适配器走真实FastAPI/PostgreSQL/JDM，不下发现场。缺条件如实INCOMPLETE，不转成mock验收成功。
- [ ] 运行 `python scripts/verify_delivery.py`、`npm.cmd run test:e2e:node`、`npm.cmd run test:e2e:dispatch-strategy`及本次tablet专项。记录源码commit/version/schema；截图检查1280×800、1024×768。首批集成R1/E/A1，后续R2/A2按通过证据合入，不合入未验收代码。
- [ ] 主任务核对实际源码diff和浏览器证据后记录各模块done，不靠子agent状态代替。对最后集成范围再做一次全分支审查，集中修复而非逐问题反复全量测试。

### Task 8: D0 — 固定制品发布1号机

**Files:** 当前版本文件/锁文件及真实部署记录；本机私有发布证据只留既有受控目录，不进入公开Git。

- [ ] 读取最新私有部署记录和当前1号机配置/应用摘要/磁盘/在途控制；不用旧源码覆盖脚本。核对授权HTTP入口，配置备份及隔离恢复验证，无需新Schema。
- [ ] 构建既有ARM64固定镜像，记录SHA/digest，按十进位递增版本；只替换原应用容器，保留host network、/dev/mqueue、DB/MQTT/Neuron，不加TLS、不清数据。
- [ ] 部署后 `/api/v1/health/live`及页面版本匹配；登录后无头沿主干只读检查、真实数据持续更新，并可见Browser抽查本次UI里程碑；不触发通知、配置发布或设备控制。
- [ ] 任一必需检查失败不宣称交付；应用失败恢复上一固定制品，保留新配置。确认新版本实际可用后报告完成、剩余真实缺口及最短下一步，更新CODEX_HANDOFF。
