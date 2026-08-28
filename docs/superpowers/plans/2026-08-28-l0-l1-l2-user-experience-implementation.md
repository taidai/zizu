# L0→L1→L2 User Experience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有节点数据功能收口为“原始点位 / 点位加工 / 实体数据”三个任务页，让实施工程师用现有主干完成一次清楚、可验证、原子发布的设备交付。

**Architecture:** 复用现有 committed-frame 快照/WebSocket、L0 历史、点位加工计划/原子应用和 L2 历史接口；不新增表、路由、依赖或运行链。后端只在现有数据主干读模型中补充安全的来源摘要、加工类型和通用扫描标记；前端删除重复的三栏总览与 L0 虚拟点位入口，实体历史改为用户展开后按需读取。

**Tech Stack:** FastAPI、Python 3.12、React 18、TypeScript、ECharts、PostgreSQL/TimescaleDB、Node 内置测试运行器

**Spec:** `docs/superpowers/specs/2026-08-28-l0-l1-l2-user-experience-design.md`

## Global Constraints

- 唯一主干保持为：`真实节点树 → L0 原始点位 → L1 点位加工 → L2 全局实体 → 上层应用`。
- 普通界面只使用“原始点位 / 点位加工 / 实体数据”；L0/L1/L2 仅作辅助标识。
- 实时与历史是同一对象的两种视图，不创建重复对象或第二套 API。
- 只消费 committed frame；不恢复数据库 latest 轮询、旧 WebSocket 或 L0 上层直读。
- 不新增依赖、数据库表、Migration、API 路由、Redis、Kafka、微服务或自由页面设计器。
- 不新增 L3；统计加工将来仍输出普通 L2。
- 不恢复 LOGICAL 虚拟点位、旧设备模板、解决方案或独立统计实体入口。
- 发布继续使用现有 plan digest、配置修订、幂等键和原子 apply seam；失败时旧修订继续工作。
- 操作员只看原始点位和实体数据；实施工程师和管理员才能进入点位加工。
- 前端权限只负责减少误操作；后端现有 `RUNTIME_READ / CONFIGURATION_READ / CONFIGURATION_WRITE` 门禁仍是权威边界。
- 本计划不伪装两项尚未实现的运行能力：统计加工运行时、使用当前 committed frame 计算候选输出值的在线试算。它们改变运行语义，分别经过专项规格和计划后才能出现在界面；本计划把现有 read-only plan/formula preview 准确称为“检查”，不称为“在线试算”。

## File Map

| 文件 | 职责 |
| --- | --- |
| `backend/app/services/point_processing.py` | 在现有 data-trunk 读模型中补来源摘要、输出加工类型和模板扫描标记 |
| `backend/tests/test_point_processing_public_api.py` | 固定操作员/工程师可见字段与模板公开契约 |
| `frontend/src/api/client.ts` | 对齐新增只读字段，不增加请求函数 |
| `frontend/src/components/data-trunk/dataTrunkViewModel.ts` | 集中三个任务页、模板推荐、质量/原因和加工类型的纯函数 |
| `frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs` | 无浏览器依赖的界面契约测试 |
| `frontend/src/pages/NodeTreePage.tsx` | 真实节点树和三个任务页的唯一导航入口 |
| `frontend/src/components/NodeTagPanel.tsx` | L0 实时/历史诊断；不再承担加工、虚拟点位和批量修改 |
| `frontend/src/components/NodeHistoryPanel.tsx` | 复用现有 L0 趋势/明细历史视图 |
| `frontend/src/components/data-trunk/DataTrunkWorkspace.tsx` | 分别编排点位加工页和实体数据页，共享一次 data-trunk/committed-frame 状态 |
| `frontend/src/components/data-trunk/PointProcessingPlanPanel.tsx` | 模板优先、确定绑定、检查和原子发布 |
| `frontend/src/components/data-trunk/NodeTrunkOverview.tsx` → `EntityDataPanel.tsx` | 删除重复 L0/L1 总览后，专门显示 L2 实时、历史和来源 |
| `frontend/src/components/data-trunk/EntityObservationCard.tsx` | 一条实体的普通信息与折叠技术详情；历史由父级按需传入 |
| `CODEX_HANDOFF.md` | 记录实现结果、门禁与下一步 |

---

### Task 1: Extend the Existing Read Model Without New Storage or Routes

**Files:**

- Modify: `backend/app/services/point_processing.py:178-220,498-554`
- Modify: `backend/tests/test_point_processing_public_api.py:216-299`
- Modify: `frontend/src/api/client.ts:979-1063`

**Interfaces:**

- Produces `PointProcessingTemplate.requires_scan: boolean`，替代前端对 `pcs.en9` 的硬编码。
- Produces `NodeDataTrunk.l1_summary.source_summary: Array<{ input_id, source_kind, source_key }>`，所有已登录角色可读，但不包含 UUID、地址或凭据。
- Produces `NodeDataTrunk.l2[].processing_kind: string | null`，来自当前已安装模板输出的 `transform.kind`。
- Preserves engineer-only `l0` 与 `input_bindings`；操作员仍不能看到绑定 UUID。

- [ ] **Step 1: Write the failing public API assertions**

在 `test_public_role_matrix_plan_apply_and_operator_projection` 的现有断言后加入：

```python
        self.assertTrue(all("requires_scan" in item for item in templates.json()["items"]))
        self.assertTrue(
            all(
                set(item) == {"input_id", "source_kind", "source_key"}
                for item in operator_trunk.json()["l1_summary"]["source_summary"]
            )
        )
        self.assertTrue(
            all("processing_kind" in item for item in operator_trunk.json()["l2"])
        )
        self.assertNotIn(
            "source_id",
            operator_trunk.json()["l1_summary"]["source_summary"][0],
        )
```

- [ ] **Step 2: Run the focused test and verify RED**

Run from `backend`:

```powershell
python -m unittest tests.test_point_processing_public_api.PointProcessingPublicApiTest.test_public_role_matrix_plan_apply_and_operator_projection -v
```

Expected: FAIL because `requires_scan`, `source_summary`, or `processing_kind` is absent.

- [ ] **Step 3: Add the minimal read-model fields**

In `PointProcessingTemplateSummary.public_dict()` add:

```python
            "requires_scan": _requires_neuron_scan(self.asset),
```

In `PointProcessingService.inspect()` build safe summaries before `l1_summary`/`l2`:

```python
        source_by_id = {item.source_id: item for item in sources}
        source_summary = []
        if current is not None:
            selected = {
                key: (value,)
                for key, value in current.input_source_ids.items()
            }
            selected.update(current.selector_source_ids)
            source_summary = [
                {
                    "input_id": input_id,
                    "source_kind": source_by_id[source_id].source_kind,
                    "source_key": source_by_id[source_id].stable_source_key,
                }
                for input_id, source_ids in sorted(selected.items())
                for source_id in source_ids
                if source_id in source_by_id
            ]

        installed_template = (
            self._catalog.get_template(current.revision_id)
            if current is not None
            else None
        )
        processing_kind_by_output = {
            output.output_id: str(output.transform.get("kind"))
            for output in installed_template.outputs
        } if installed_template is not None else {}
```

Add `"source_summary": source_summary` to `l1_summary` for every role, and add this field to every L2 item:

```python
                        "processing_kind": processing_kind_by_output.get(key),
```

Do not move `input_bindings` outside the existing `include_engineering` guard.

- [ ] **Step 4: Update the frontend contract**

Add to `PointProcessingTemplate`:

```typescript
  requires_scan: boolean
```

Add to `NodeDataTrunk.l1_summary`:

```typescript
    source_summary: Array<{
      input_id: string
      source_kind: 'l0' | 'l2'
      source_key: string
    }>
```

Add to each `NodeDataTrunk.l2` item:

```typescript
    processing_kind: string | null
```

Name the existing history range union once and reuse it in Task 5:

```typescript
export type EntityHistoryRange = '1h' | '6h' | '24h' | '7d'
```

Change `fetchEntityInstanceHistory()` to accept `range: EntityHistoryRange = '1h'`; the URL and response shape remain unchanged.

- [ ] **Step 5: Run focused backend tests and verify GREEN**

Run from `backend`:

```powershell
python -m unittest tests.test_point_processing_public_api tests.test_point_processing -v
```

Expected: all tests PASS; operator projection still has `l0 == []` and no `input_bindings`.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/services/point_processing.py backend/tests/test_point_processing_public_api.py frontend/src/api/client.ts
git commit -m "feat(data-trunk): expose safe processing summaries"
```

---

### Task 2: Freeze the Three-Page UX Contract in Pure Functions

**Files:**

- Modify: `frontend/src/components/data-trunk/dataTrunkViewModel.ts`
- Modify: `frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs`

**Interfaces:**

- Produces `nodeDataTabs(readOnly): readonly NodeDataTab[]`.
- Produces `recommendPointProcessingTemplate(templates, l0, installedRevisionId): string`.
- Produces `processingKindLabel(kind): '即时' | '统计'`.
- Produces `entityReasonLabel(reason, ageMs): string | null`.
- Consumed by Tasks 3-5; no React state is hidden in this module.

- [ ] **Step 1: Write failing view-model tests**

Append these cases to `dataTrunkViewModel.test.mjs`:

```javascript
test('node data tabs use task names and hide processing from operators', async () => {
  const model = await import('./dataTrunkViewModel.ts')
  assert.deepEqual(
    model.nodeDataTabs(false).map((item) => [item.key, item.label]),
    [
      ['raw-points', '原始点位'],
      ['point-processing', '点位加工'],
      ['entities', '实体数据'],
    ],
  )
  assert.deepEqual(
    model.nodeDataTabs(true).map((item) => item.key),
    ['raw-points', 'entities'],
  )
})

test('template recommendation keeps installed revision then prefers exact coverage', async () => {
  const model = await import('./dataTrunkViewModel.ts')
  const templates = [
    { revision_id: 'a', revision: 1, inputs: [{ source_kind: 'l0', source_key: 'P', aliases: [], data_type: 'FLOAT', unit: 'W', required: true }] },
    { revision_id: 'b', revision: 1, inputs: [{ source_kind: 'l0', source_key: 'Power', aliases: ['PAct'], data_type: 'FLOAT', unit: 'kW', required: true }] },
  ]
  const l0 = [{ source_key: 'Power', data_type: 'FLOAT', unit: 'kW' }]
  assert.equal(model.recommendPointProcessingTemplate(templates, l0, 'a'), 'a')
  assert.equal(model.recommendPointProcessingTemplate(templates, l0, null), 'b')
})

test('entity reason is human readable and technical kind stays a label', async () => {
  const model = await import('./dataTrunkViewModel.ts')
  assert.equal(model.processingKindLabel('window'), '统计')
  assert.equal(model.processingKindLabel('formula'), '即时')
  assert.equal(model.entityReasonLabel('FRAME_PROCESSING_FAILED', 0), '本次点位加工失败，当前值不可用')
  assert.equal(model.entityReasonLabel('STALE', 17 * 60_000), '原始数据已 17 分钟未更新')
})
```

- [ ] **Step 2: Run the model test and verify RED**

Run from `frontend`:

```powershell
node --test --experimental-strip-types src/components/data-trunk/dataTrunkViewModel.test.mjs
```

Expected: FAIL because the four functions do not exist.

- [ ] **Step 3: Implement the smallest deterministic model**

Add these public contracts to `dataTrunkViewModel.ts`:

```typescript
export type NodeDataTabKey = 'raw-points' | 'point-processing' | 'entities'

export interface NodeDataTab {
  key: NodeDataTabKey
  label: string
}

export function nodeDataTabs(readOnly: boolean): readonly NodeDataTab[] {
  const tabs: NodeDataTab[] = [
    { key: 'raw-points', label: '原始点位' },
    { key: 'point-processing', label: '点位加工' },
    { key: 'entities', label: '实体数据' },
  ]
  return readOnly ? tabs.filter((item) => item.key !== 'point-processing') : tabs
}

interface TemplateCandidate {
  revision_id: string
  revision: number
  inputs: Array<{
    source_kind: 'l0' | 'l2'
    source_key: string
    aliases: string[]
    data_type: string
    unit: string | null
    required: boolean
  }>
}

interface L0Candidate {
  source_key: string
  data_type: string
  unit: string | null
}

export function recommendPointProcessingTemplate(
  templates: TemplateCandidate[],
  l0: L0Candidate[],
  installedRevisionId: string | null,
): string {
  if (installedRevisionId && templates.some((item) => item.revision_id === installedRevisionId)) {
    return installedRevisionId
  }
  const score = (template: TemplateCandidate) => template.inputs.reduce((total, input) => {
    if (input.source_kind !== 'l0') return total
    const keys = new Set([input.source_key, ...input.aliases].map((item) => item.toLocaleLowerCase()))
    const matches = l0.filter((source) => (
      keys.has(source.source_key.toLocaleLowerCase())
      && source.data_type === input.data_type
      && (source.unit || null) === (input.unit || null)
    ))
    return total + (matches.length === 1 ? 10 : matches.length > 1 ? 1 : input.required ? -100 : 0)
  }, 0)
  return [...templates]
    .sort((left, right) => score(right) - score(left) || right.revision - left.revision || left.revision_id.localeCompare(right.revision_id))[0]?.revision_id || ''
}

export function processingKindLabel(kind: string | null): '即时' | '统计' {
  return kind === 'window' || kind === 'metric' || kind === 'statistics' ? '统计' : '即时'
}

export function entityReasonLabel(reason: string | null, ageMs: number): string | null {
  if (reason === 'FRAME_PROCESSING_FAILED') return '本次点位加工失败，当前值不可用'
  if (reason === 'STALE' || reason === 'ENTITY_DATA_STALE') {
    const minutes = Math.max(1, Math.floor(ageMs / 60_000))
    return `原始数据已 ${minutes} 分钟未更新`
  }
  if (reason === 'ENTITY_DATA_QUALITY_BAD') return '原始数据质量异常，当前值不可用'
  return reason ? '当前值不可用，请展开技术详情' : null
}
```

- [ ] **Step 4: Run tests and verify GREEN**

Run from `frontend`:

```powershell
node --test --experimental-strip-types src/components/data-trunk/dataTrunkViewModel.test.mjs
```

Expected: all model tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/components/data-trunk/dataTrunkViewModel.ts frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs
git commit -m "test(ui): freeze node data task model"
```

---

### Task 3: Replace the Mixed Node Screen With Task Navigation and a Read-Only L0 Page

**Files:**

- Modify: `frontend/src/pages/NodeTreePage.tsx:10,435-445,620-728`
- Modify: `frontend/src/components/NodeTagPanel.tsx`
- Reuse unchanged: `frontend/src/components/NodeHistoryPanel.tsx`

**Interfaces:**

- Consumes `nodeDataTabs(readOnly)` from Task 2.
- Produces `NodeTreePage` task routes: `raw-points`, `point-processing`, `entities`.
- Produces one `NodeTagPanel` with internal `realtime | history` view; it exposes no L1-style processing controls.

- [ ] **Step 1: Change the navigation contract before changing render branches**

Replace the local `TabKey` and hard-coded tab arrays in `NodeTreePage.tsx`:

```typescript
import { nodeDataTabs, type NodeDataTabKey } from '../components/data-trunk/dataTrunkViewModel'

const [activeTab, setActiveTab] = useState<NodeDataTabKey>('raw-points')
```

Render tabs with:

```tsx
{nodeDataTabs(readOnly).map((tab) => (
  <button
    key={tab.key}
    type="button"
    onClick={() => setActiveTab(tab.key)}
    className={activeTab === tab.key ? activeClass : inactiveClass}
  >
    {tab.label}
  </button>
))}
```

Remove the “节点概览” task tab; node metadata remains in the existing node header and edit dialog.

- [ ] **Step 2: Render exactly one component per task**

Replace the old render branches with:

```tsx
{activeTab === 'raw-points' && <NodeTagPanel nodeId={selectedNode.id} />}
{activeTab === 'point-processing' && !readOnly && (
  <DataTrunkWorkspace node={selectedNode} readOnly={false} actorId={actorId} view="processing" />
)}
{activeTab === 'entities' && (
  <DataTrunkWorkspace node={selectedNode} readOnly={readOnly} actorId={actorId} view="entities" />
)}
```

When `readOnly` changes to true while `activeTab === 'point-processing'`, reset it to `raw-points` in an effect.

- [ ] **Step 3: Reduce `NodeTagPanel` to diagnosis only**

Keep these existing concerns:

```typescript
type RawPointView = 'realtime' | 'history'

const [view, setView] = useState<RawPointView>('realtime')
const [tags, setTags] = useState<Tag[]>([])
const [projection, setProjection] = useState<CommittedFrameProjection | null>(null)
```

Keep `fetchTags`, committed snapshot/WebSocket, search, data-type filter and pagination. Remove imports, state, handlers and JSX for:

```text
EditableCell
batchUpdateTags / updateTag / deleteTag / createTag
批量 Scale / Offset / Unit / 节点移动
新建点位 / 编辑 / 删除
PHYSICAL / LOGICAL 筛选
高级虚拟点位、公式、聚合和跨节点来源选择
```

The Neuron import button remains in `NodeTreePage` and is the one L0 provisioning path visible to implementation engineers.

- [ ] **Step 4: Put realtime and history in the same page**

Use a small two-button switch:

```tsx
<div className="flex gap-2">
  <button type="button" onClick={() => setView('realtime')}>实时</button>
  <button type="button" onClick={() => setView('history')}>历史</button>
</div>
```

Keep the realtime table inline rather than creating another component:

```tsx
{view === 'history' ? (
  <NodeHistoryPanel nodeId={nodeId} />
) : (
  <table className="w-full text-xs">
    <thead>
      <tr>
        {['点位名称', '当前值', '单位', '质量', '数据时间', '来源'].map((label) => (
          <th key={label}>{label}</th>
        ))}
      </tr>
    </thead>
    <tbody>
      {tags.map((tag) => {
        const current = projection?.l0.get(tag.id)
        return (
          <tr key={tag.id}>
            <td>{tag.display_name || tag.name}</td>
            <td>{current?.value === null || current?.value === undefined ? '—' : String(current.value)}</td>
            <td>{current?.unit || tag.unit || '—'}</td>
            <td>{qualityLabel(current?.effective_quality ?? 1)}</td>
            <td>{current?.source_timestamp ? new Date(current.source_timestamp).toLocaleString('zh-CN') : '未收到'}</td>
            <td>{current?.source_path || tag.source_path || '未记录'}</td>
          </tr>
        )
      })}
    </tbody>
  </table>
)}
```

Its visible columns are exactly:

```text
点位名称｜当前值｜单位｜质量｜数据时间｜来源
```

Current value, quality and time come from `projection.l0.get(tag.id)`; tag metadata supplies name and fallback source path. Quality labels use `qualityLabel()` from Task 2. Do not display editable scale, offset or derived engineering value.

- [ ] **Step 5: Verify model and production build**

Run from `frontend`:

```powershell
node --test --experimental-strip-types src/components/data-trunk/dataTrunkViewModel.test.mjs src/components/data-trunk/committedFrameProjection.test.mjs
npm run build
```

Expected: tests PASS and Vite production build exits 0.

- [ ] **Step 6: Commit**

```powershell
git add frontend/src/pages/NodeTreePage.tsx frontend/src/components/NodeTagPanel.tsx
git commit -m "refactor(ui): focus raw point workspace"
```

---

### Task 4: Turn Existing Point Processing Into One Guided Page

**Files:**

- Modify: `frontend/src/components/data-trunk/DataTrunkWorkspace.tsx`
- Modify: `frontend/src/components/data-trunk/PointProcessingPlanPanel.tsx`
- Modify: `frontend/src/components/data-trunk/PointProcessingFormulaEditor.tsx`
- Modify: `frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs`

**Interfaces:**

- Consumes `view="processing"`, `recommendPointProcessingTemplate()` and `PointProcessingTemplate.requires_scan`.
- Preserves existing `createPointProcessingPlan()` then `applyPointProcessingPlan()` semantics.
- Produces two user actions: read-only “检查加工结果” and atomic “检查并发布”.

- [ ] **Step 1: Add the workspace mode and stop loading irrelevant entity history**

Extend the component props:

```typescript
view: 'processing' | 'entities'
```

In `loadRuntime()`, always call `fetchNodeDataTrunk(node.id)`, but only call `fetchEntityInstances()` when `view === 'entities'`. Remove the `Promise.all(nextTrunk.l2.map(fetchEntityInstanceHistory))` fan-out entirely; Task 5 loads one selected entity on demand.

- [ ] **Step 2: Select an explicit recommendation without hiding manual choice**

After templates and trunk load, choose:

```typescript
const recommended = recommendPointProcessingTemplate(
  nextTemplates,
  nextTrunk.l0,
  nextTrunk.l1_summary.revision_id,
)
setSelectedRevisionId((current) => current || recommended)
```

In the template `<select>`, keep all matching device-category templates, show `品牌 / 型号 / 修订`, and add “推荐” only beside the recommended revision. Manual selection remains available.

- [ ] **Step 3: Remove the EN9 special case**

Replace:

```typescript
const scanDriven = selectedTemplate?.asset_id === 'pcs.en9'
```

with:

```typescript
const scanDriven = selectedTemplate?.requires_scan ?? false
```

No brand or product ID may be hard-coded in the generic page.

- [ ] **Step 4: Present input, rule and output as one processing workspace**

Change `PointProcessingPlanPanel` from a narrow `<aside>` to a full-width section. Keep existing controls but group them into this grid:

```tsx
<section className="grid gap-3 xl:grid-cols-3">
  <div aria-label="输入点位">{/* deterministic L0/L2 bindings */}</div>
  <div aria-label="加工规则">{/* selected template and formula check */}</div>
  <div aria-label="输出预览">{/* plan blockers, changes, DAG, stable L2 IDs */}</div>
</section>
```

Use these exact action labels:

```text
检查加工结果
检查并发布
正在检查…
正在发布…
```

Do not label the existing plan/formula preview as “在线试算”, because it does not execute candidate outputs against the current committed frame.

- [ ] **Step 5: Keep one atomic publish action and the current retry evidence**

Do not merge the two HTTP calls. “检查加工结果” creates a read-only plan; “检查并发布” calls the existing apply seam with plan digest and stored idempotency key. Keep `DataTrunkResultUnknownError` and the same-request retry behavior unchanged.

After successful apply, reload the trunk and render:

```text
发布成功｜配置修订 {n}｜生成 {count} 个实体
```

On any blocker or apply failure, keep the old installed revision visible and do not clear it from the page.

- [ ] **Step 6: Update copy that still exposes old concepts**

Replace “当前解决方案没有适用于……” with:

```text
当前设备类型没有可用的点位加工模板。
```

Replace ordinary “全局实体” headings with “实体”; keep `L2` and revision IDs inside technical details or plan diagnostics.

- [ ] **Step 7: Verify frontend checks**

Run from `frontend`:

```powershell
node --test --experimental-strip-types src/components/data-trunk/*.test.mjs
npm run build
```

Expected: all Node tests PASS and build exits 0.

- [ ] **Step 8: Commit**

```powershell
git add frontend/src/components/data-trunk/DataTrunkWorkspace.tsx frontend/src/components/data-trunk/PointProcessingPlanPanel.tsx frontend/src/components/data-trunk/PointProcessingFormulaEditor.tsx frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs
git commit -m "refactor(ui): guide point processing publish"
```

---

### Task 5: Make Entity Realtime, History and Evidence One Simple View

**Files:**

- Rename: `frontend/src/components/data-trunk/NodeTrunkOverview.tsx` → `frontend/src/components/data-trunk/EntityDataPanel.tsx`
- Modify: `frontend/src/components/data-trunk/DataTrunkWorkspace.tsx`
- Modify: `frontend/src/components/data-trunk/EntityObservationCard.tsx`
- Modify: `frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs`

**Interfaces:**

- Consumes committed L2 projection, `NodeDataTrunk.l2[].processing_kind`, `l1_summary.source_summary`, and the existing per-entity history API.
- Produces only one entity list; “即时/统计” is a label, never a second catalog.
- Produces at most one history request at a time.

Use this exact parent/child boundary:

```typescript
interface EntityDataPanelProps {
  trunk: NodeDataTrunk
  descriptors: Map<string, EntityInstance>
  projection: CommittedFrameProjection | null
  selectedEntityId: string | null
  selectedRange: EntityHistoryRange
  history: EntityInstanceObservation[]
  historyLoading: boolean
  onSelectEntity: (entityId: string) => void
  onRangeChange: (range: EntityHistoryRange) => void
}

interface EntityObservationCardProps {
  descriptor: EntityInstance
  observation: L2FrameItem | null
  processingKind: string | null
  sourceSummary: NodeDataTrunk['l1_summary']['source_summary']
  expanded: boolean
  selectedRange: EntityHistoryRange
  history: EntityInstanceObservation[]
  historyLoading: boolean
  onToggle: () => void
  onRangeChange: (range: EntityHistoryRange) => void
}
```

- [ ] **Step 1: Rename the component because its old responsibility is deleted**

Rename the file and export:

```typescript
export default function EntityDataPanel(/* existing runtime props plus selection props */) {
```

Delete the old L0 and L1 columns from the component. This rename is intentional: the three-layer overview is no longer a product page, and leaving an unused duplicate would invite the old design back.

- [ ] **Step 2: Add one selected-entity history state in `DataTrunkWorkspace`**

Use:

```typescript
const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null)
const [entityRange, setEntityRange] = useState<EntityHistoryRange>('1h')
const [entityHistory, setEntityHistory] = useState<EntityInstanceObservation[]>([])
```

Only call `fetchEntityInstanceHistory(selectedEntityId, entityRange)` when the user expands an entity or changes its range. Clear selection/history when `node.id` changes. Do not prefetch history for every entity.

- [ ] **Step 3: Render realtime first, history on demand**

Each entity row/card shows:

```text
实体名称｜当前值｜单位｜质量｜更新时间｜即时/统计
```

Clicking it reveals “历史 / 来源 / 技术详情”. History range buttons use only the existing supported values `1h / 6h / 24h / 7d`; no “全部” option and no client-side fake aggregation.

- [ ] **Step 4: Show human evidence before technical evidence**

The default source section uses `l1_summary.source_summary`:

```tsx
<p>
  来源：{sourceSummary.map((item) => item.source_key).join('、') || '等待来源'}
</p>
<p>加工：{processingKindLabel(item.processing_kind)}</p>
```

Move these existing fields under a native `<details>` element labelled “技术详情”:

```text
definition_id
processing_revision_id
configuration_revision
source_digest
frame_sequence
received_at
calculated_at
```

No dialog library or new dependency is needed.

- [ ] **Step 5: Translate runtime reasons without discarding the code**

Use `entityReasonLabel(observation.reason, ageMs)` for the visible sentence. Keep the raw `reason` only inside technical details. STALE/BAD retains the last value visually but marks it unavailable; controls continue to rely on backend quality gates.

- [ ] **Step 6: Verify request count and build manually**

Run from `frontend`:

```powershell
node --test --experimental-strip-types src/components/data-trunk/*.test.mjs
npm run build
```

Then open the local node page and verify in browser network tools:

```text
进入实体数据：0 个 entity history 请求
展开一个实体：1 个 entity history 请求
切换范围：仅该实体新增 1 个请求
切换节点：旧实体历史不再显示
```

- [ ] **Step 7: Commit**

```powershell
git add frontend/src/components/data-trunk/NodeTrunkOverview.tsx frontend/src/components/data-trunk/EntityDataPanel.tsx frontend/src/components/data-trunk/DataTrunkWorkspace.tsx frontend/src/components/data-trunk/EntityObservationCard.tsx frontend/src/components/data-trunk/dataTrunkViewModel.test.mjs
git commit -m "refactor(ui): unify entity realtime and history"
```

---

### Task 6: Run the Full Gate and Record the Delivered Boundary

**Files:**

- Modify: `CODEX_HANDOFF.md`
- Modify only if actual user-facing behavior changed from the accepted text: `docs/superpowers/specs/2026-08-28-l0-l1-l2-user-experience-design.md`

**Interfaces:**

- Consumes all prior tasks.
- Produces a verified local candidate; it does not build or deploy a release image.

- [ ] **Step 1: Run all frontend contract tests and production build**

Run from `frontend`:

```powershell
node --test --experimental-strip-types src/components/data-trunk/*.test.mjs
npm run build
```

Expected: all tests PASS; TypeScript and Vite build exit 0.

- [ ] **Step 2: Run focused backend tests**

Run from `backend`:

```powershell
python -m unittest tests.test_point_processing_public_api tests.test_point_processing tests.test_entity_instance_l2_runtime -v
```

Expected: all focused tests PASS.

- [ ] **Step 3: Run the repository-required backend gate**

Run from `backend`:

```powershell
python -m unittest discover -s tests -v
python -m compileall app
```

Expected: suite completes with 0 failures; compileall exits 0. Record any intentional skips exactly rather than describing them as passes.

- [ ] **Step 4: Perform the four-step local smoke**

Using existing local test data and an engineer login, verify:

```text
选择节点 → 原始点位实时 → 原始点位历史
选择节点 → 点位加工 → 推荐模板 → 检查加工结果
阻断计划不能发布；ready 计划显示“检查并发布”
选择节点 → 实体数据 → 展开一个实体 → 切换历史范围 → 展开技术详情
```

Using an operator login, verify “点位加工” is absent and no configuration request is issued.

- [ ] **Step 5: Record the exact boundary in handoff**

Append a dated section to `CODEX_HANDOFF.md` containing:

```text
已交付：三个任务页、L0 实时/历史、模板优先检查/原子发布、L2 实时/按需历史/来源摘要。
未冒充已交付：统计加工运行时、候选配置对当前 committed frame 的在线值试算。
验证证据：前端测试数量、build 退出码、后端通过/跳过/失败数量、浏览器 smoke 结果。
```

- [ ] **Step 6: Check documentation and commit the verified slice**

Run from repository root:

```powershell
$forbidden = @('TO' + 'DO', 'TB' + 'D', '待' + '定', '待' + '确认', '解决方案没有', 'L0 → L1 → L2', 'L0 点位实时')
rg -n ($forbidden -join '|') frontend/src docs/superpowers/plans/2026-08-28-l0-l1-l2-user-experience-implementation.md
git diff --check
git status --short
```

Expected: no stale user-facing copy from this plan, no whitespace errors, and only intended files are modified.

Commit:

```powershell
git add CODEX_HANDOFF.md docs/superpowers/specs/2026-08-28-l0-l1-l2-user-experience-design.md
git commit -m "docs: record node data workspace convergence"
```

If the spec did not require correction and is unchanged, stage only `CODEX_HANDOFF.md`.

## Deferred Subprojects Required by the Accepted Spec

The accepted design also names capabilities that are not present in the current runtime or current product path. They are deliberately absent from this implementation plan so the first slice stays testable and does not mix UI cleanup with new industrial execution semantics:

1. **Online value trial:** evaluate an unapplied point-processing plan against one immutable committed frame, return typed candidate L2 values and source evidence, and persist nothing.
2. **Statistical processing:** define window, method and timezone as an L1 transform; maintain recoverable current-window projection and immutable L2 history results without creating a statistical-entity layer.
3. **Shared template lifecycle:** admin import/catalog, reusable instance adjustments, immutable upgrade diff and explicit revision adoption. The current slice uses already-imported templates and never auto-upgrades a running device.
4. **Long-range L2 history:** query ranges beyond 7 days with server-selected minute/hour/day aggregates. The current slice exposes only the existing bounded raw-history ranges and does not aggregate in the browser.
5. **Explicit brand/series metadata:** add only if real delivery proves exact device-category and point-contract matching cannot recommend the right template; the current slice prefers observed point contracts over a new manually maintained node field.

None may be enabled by copy-only UI. Each starts with its own accepted specification, TDD plan, failure semantics and PostgreSQL verification before being attached to these pages.
