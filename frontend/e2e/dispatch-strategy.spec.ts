import { expect, test, type Page, type Route } from '@playwright/test'
import { execFile, spawn, type ChildProcess } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { promisify } from 'node:util'
import { buildTwoChargeTwoDischargeJdm } from '../src/components/dispatch-strategy/dispatchStrategyModel.mjs'
import { buildGenericDecisionTableJdm } from '../src/components/dispatch-strategy/nativeDecisionTableModel'
import { stopSpawnedProcess } from './support/localProcess.mjs'
import { openEngineeringPage } from './support/tabletNavigation'
import { localDatabaseEnvironment } from './support/localDatabase.mjs'

const execFileAsync = promisify(execFile)
test.use({ actionTimeout: 10_000 })
let localFixtureOutput = ''

const now = '2026-09-05T00:00:00+00:00'
const entities = [
  { id: 'entity-soc', node_id: 'node-ess', node_type: 'ESS', node_display_name: '1#储能', definition_id: 'bms.soc', display_name: 'SOC', data_type: 'FLOAT', unit: '%', direction: 'R', freshness_seconds: 10, confirmed: true },
  { id: 'entity-limit', node_id: 'node-pcs', node_type: 'PCS', node_display_name: '1#PCS', definition_id: 'pcs.max_discharge_limit', display_name: '最大放电功率限值', data_type: 'FLOAT', unit: 'kW', direction: 'RW', freshness_seconds: 10, confirmed: true, control_eligible: true },
]

function revision(id: string, lifecycle: 'DRAFT' | 'PUBLISHED', body: any = {}) {
  return {
    id,
    strategy_id: 'strategy-1',
    revision: lifecycle === 'DRAFT' ? 1 : 2,
    lifecycle,
    trigger_kind: body.trigger_kind || 'FIXED_TICK',
    site_timezone: body.site_timezone || 'Asia/Shanghai',
    jdm_content: body.jdm_content || buildTwoChargeTwoDischargeJdm([
      { key: 'charge-1', start: '00:00', end: '06:00', action: 'CHARGE', target: 0, socMin: 10, socMax: 90 },
      { key: 'discharge-1', start: '10:00', end: '12:00', action: 'DISCHARGE', target: 0, socMin: 10, socMax: 90 },
      { key: 'charge-2', start: '12:00', end: '14:00', action: 'CHARGE', target: 0, socMin: 10, socMax: 90 },
      { key: 'discharge-2', start: '18:00', end: '22:00', action: 'DISCHARGE', target: 0, socMin: 10, socMax: 90 },
    ], 0),
    content_digest: lifecycle === 'DRAFT' ? 'b'.repeat(64) : 'c'.repeat(64),
    base_configuration_revision: 7,
    bindings: body.bindings || [],
    created_by: 'engineer:e2e',
    created_at: now,
    published_by: lifecycle === 'PUBLISHED' ? 'engineer:e2e' : null,
    published_at: lifecycle === 'PUBLISHED' ? now : null,
  }
}

function strategyView() {
  return {
    id: 'strategy-1', name: '2充2放调度策略', description: null,
    active_revision_id: null, enabled: false, runtime_health: 'IDLE',
    last_trigger_key: null, last_evaluated_at: null, last_desired: null,
    last_actual: null, last_evidence: null, failure_code: null,
    created_at: now, updated_at: now, draft: revision('draft-1', 'DRAFT'),
    active_revision: null, published_revision: null,
  } as any
}

async function installApi(page: Page, initialStrategy: any = null, entityRows = entities, currentConfigurationRevision = 7) {
  await page.addInitScript(() => {
    class IsolatedStream {
      static OPEN = 1
      readyState = 1
      onopen: ((event: Event) => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      constructor() { setTimeout(() => this.onopen?.(new Event('open')), 0) }
      send(value: string) {
        const request = JSON.parse(value)
        const type = request.authenticate ? 'authenticated' : request.subscribe ? 'subscribed' : null
        if (type) setTimeout(() => this.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type }) })), 0)
      }
      close() { this.readyState = 3 }
    }
    Object.defineProperty(window, 'WebSocket', { value: IsolatedStream })
  })
  let strategy: any = initialStrategy
  let events: any[] = []
  const calls: string[] = []
  const savedDrafts: any[] = []
  const json = (route: Route, value: unknown, status = 200) => route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(value),
  })
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname.replace('/api/v1', '')
    const method = request.method()
    calls.push(`${method} ${path}`)
    if (path === '/auth/me') return json(route, { user: { id: 'engineer-1', username: 'e2e-engineer', role: 'engineer' } })
    if (path === '/health') return json(route, { version: '0.8.5', pipeline: { status: 'running', messages_received: 10, points_written_db: 10, last_message_at: now }, components: { mqtt: { status: 'connected' } } })
    if (path === '/ems-workbench') return json(route, { workbench_id: 'default', configuration_revision: 7, navigation: [], groups: [], kpis: [], trends: [], alarms: { visible: true }, controls: { visible: false, entities: [] } })
    if (path === '/nodes') return json(route, { nodes: [] })
    if (path === '/categories') return json(route, { categories: [] })
    if (path === '/alarms/counts') return json(route, { counts: {} })
    if (path === '/auth/ws-ticket') return json(route, { ticket: 'isolated-strategy-ui' })
    if (path === '/runtime/frame-snapshot') return json(route, {
      type: 'frame_snapshot', node_id: url.searchParams.get('node_id'), cursor: 'isolated',
      frame_sequence: 0, frame_time: null, configuration_revision: 7, frame_status: null,
      failure: null, backlog_frames: 0, l0: [], l2: [],
    })
    if (path === '/entity-instances' && method === 'GET') return json(route, { items: entityRows, total: entityRows.length })
    if (path.endsWith('/realtime') && method === 'GET') {
      const id = path.split('/')[2]
      return json(route, { entity_instance_id: id, definition_id: id === 'entity-soc' ? 'bms.soc' : 'pcs.max_discharge_limit', node_id: 'node-1', node_key: 'node', value: id === 'entity-soc' ? 50 : 156.8, data_type: 'FLOAT', unit: id === 'entity-soc' ? '%' : 'kW', observed_at: now, quality: 192, age_ms: 0, fresh: true, quality_good: true, processing_revision_id: 'processing-1', configuration_revision: 7 })
    }
    if (path === '/dispatch-strategies' && method === 'GET') return json(route, { strategies: strategy ? [strategy] : [] })
    if (path === '/dispatch-strategies' && method === 'POST') {
      strategy = strategyView()
      return json(route, strategy)
    }
    if (path === '/dispatch-strategies/strategy-1' && method === 'GET') return json(route, strategy)
    if (path === '/dispatch-strategies/strategy-1/events' && method === 'GET') {
      const offset = Number(url.searchParams.get('cursor') || 0)
      const limit = Number(url.searchParams.get('limit') || 10)
      return json(route, { items: events.slice(offset, offset + limit), next_cursor: offset + limit < events.length ? String(offset + limit) : null })
    }
    if (path === '/dispatch-strategies/strategy-1/draft' && method === 'PUT') {
      const body = request.postDataJSON()
      savedDrafts.push(body)
      const refreshedBindings = body.bindings.map((binding: any) => {
        const entity = entityRows.find((item: any) => item.id === binding.entity_instance_id)
        return entity ? { ...binding, expected_data_type: entity.data_type.toUpperCase(), unit: entity.unit } : binding
      })
      strategy = {
        ...strategy,
        name: body.name,
        runtime_health: 'READY',
        draft: revision('draft-1', 'DRAFT', {
          ...body,
          base_configuration_revision: currentConfigurationRevision,
          bindings: refreshedBindings,
        }),
      }
      return json(route, strategy)
    }
    if (path === '/dispatch-strategies/strategy-1/simulate' && method === 'POST') return json(route, {
      status: 'EVALUATED', reason_code: null, frame_sequence: 42, configuration_revision: 7,
      snapshot: { soc: { value: 50, quality: 'GOOD' }, 'power-target': { value: 156.8, quality: 'GOOD' } },
      engine_inputs: { soc: 50, site_local_minute: 600 }, matched_rules: ['discharge-1'],
      decision: { action_id: 'power-target', target: 80, matched_rule: 'discharge-1' },
      proposed_intents: [{ action_id: 'power-target', entity_instance_id: 'entity-limit', value: 80, ordinal: 0 }],
    })
    if (path === '/dispatch-strategies/strategy-1/publish' && method === 'POST') {
      const published = revision('published-1', 'PUBLISHED', strategy.draft)
      strategy = { ...strategy, draft: null, published_revision: published, runtime_health: 'READY' }
      return json(route, published)
    }
    if (path === '/dispatch-strategies/strategy-1/enable' && method === 'POST') {
      strategy = { ...strategy, enabled: true, active_revision_id: 'published-1', active_revision: strategy.published_revision, runtime_health: 'READY' }
      events = [{ id: 'event-1', occurred_at: now, event_kind: 'DECISION_CHANGED', trigger_kind: 'FIXED_TICK', trigger_key: 'tick:1', frame_sequence: 42, configuration_revision: 7, snapshot_evidence: {}, decision: { matched_rule: 'discharge-1' }, intent_summary: [{ value: 80 }], control_command_id: 'command-1', control_status: 'dispatched', reason_code: null }]
      return json(route, strategy)
    }
    if (path === '/dispatch-strategies/strategy-1/disable' && method === 'POST') {
      strategy = { ...strategy, enabled: false }
      return json(route, strategy)
    }
    if (path === '/dispatch-strategies/strategy-1/failure-latch/clear' && method === 'POST') {
      strategy = { ...strategy, runtime_health: 'READY', failure_code: null }
      return json(route, strategy)
    }
    if (path === '/control-commands/command-1' && method === 'GET') return json(route, { id: 'command-1', status: 'dispatched', code: 'WAITING_READBACK', source_type: 'strategy' })
    return json(route, { detail: { code: 'UNMOCKED', message: `${method} ${path}` } }, 500)
  })
  return { calls, savedDrafts, getStrategy: () => strategy, setEvents: (items: any[]) => { events = items } }
}

for (const viewport of [{ width: 1280, height: 800 }, { width: 1024, height: 768 }]) test(`Demo布局首屏三步横排与全宽决策表 ${viewport.width}`, async ({ page }) => {
  const original = strategyView()
  original.draft.jdm_content = buildGenericDecisionTableJdm()
  await installApi(page, original)
  await page.setViewportSize(viewport)
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  const steps = page.getByTestId('dispatch-steps')
  await expect(steps).toBeInViewport()
  const boxes = await steps.locator(':scope > *').evaluateAll((elements) => elements.map((element) => { const r = element.getBoundingClientRect(); return { top: r.top, right: r.right, left: r.left } }))
  expect(boxes).toHaveLength(3)
  expect(Math.max(...boxes.map((box) => box.top)) - Math.min(...boxes.map((box) => box.top))).toBeLessThan(2)
  expect(boxes[0].right).toBeLessThanOrEqual(boxes[1].left)
  expect(boxes[1].right).toBeLessThanOrEqual(boxes[2].left)
  const table = page.getByTestId('native-decision-table')
  await expect(table).toBeInViewport()
  expect((await table.boundingBox())!.width).toBeGreaterThan(viewport.width * 0.85)
  for (const label of ['保存草稿', '试算', '发布', '启用']) await expect(page.getByRole('button', { name: label, exact: true })).toBeInViewport()
  await expect(page.getByLabel('输入 1 别名')).not.toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: test.info().outputPath(`demo-layout-${viewport.width}.png`) })
})

test('绑定弹窗取消不改草稿，应用才使旧试算与收据失效', async ({ page }) => {
  const original = strategyView()
  original.draft.jdm_content = buildGenericDecisionTableJdm()
  original.draft.bindings = [
    { direction: 'INPUT', binding_key: 'soc', ordinal: 0, entity_instance_id: 'entity-soc', expected_data_type: 'FLOAT', unit: '%', freshness_seconds: 10 },
    { direction: 'OUTPUT', binding_key: 'power_target', ordinal: 0, entity_instance_id: 'entity-limit', expected_data_type: 'FLOAT', unit: 'kW', freshness_seconds: 10 },
  ]
  const api = await installApi(page, original)
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await page.getByRole('button', { name: '试算', exact: true }).click()
  await expect(page.getByTestId('strategy-simulation')).toBeVisible()
  await page.getByRole('button', { name: /01.*选择 L2 输入/ }).click()
  await page.getByLabel('输入 1 别名').fill('changed_alias')
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await expect(page.getByRole('button', { name: /01.*选择 L2 输入/ })).toBeFocused()
  await expect(page.getByTestId('strategy-simulation')).toBeVisible()
  await expect(page.getByTestId('strategy-draft-receipt')).toBeVisible()
  await page.getByRole('button', { name: /01.*选择 L2 输入/ }).click()
  await expect(page.getByLabel('输入 1 别名')).not.toHaveValue('changed_alias')
  await page.getByLabel('输入 1 别名').fill('changed_alias')
  await page.getByRole('button', { name: '应用绑定', exact: true }).click()
  await expect(page.getByRole('dialog')).not.toBeVisible()
  await expect(page.getByTestId('strategy-simulation')).not.toBeVisible()
  await expect(page.getByTestId('strategy-draft-receipt')).not.toBeVisible()
  expect(api.savedDrafts).toHaveLength(1)
})

test('按需预览、同一JDM草稿与表达式说明不修改或执行策略', async ({ page }) => {
  const original = strategyView()
  original.draft.jdm_content = buildGenericDecisionTableJdm()
  const api = await installApi(page, original)
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await page.getByRole('button', { name: '草稿 JSON', exact: true }).click({ timeout: 3000 })
  await expect(page.getByRole('dialog')).toContainText('intents')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: '草稿 JSON', exact: true })).toBeFocused()
  await page.getByRole('button', { name: '输入/输出预览', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('未绑定')
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '表达式说明', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('action_id')
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: '保存草稿', exact: true })).toBeEnabled()
  expect(api.calls.filter((call) => /^(PUT|POST) \/(dispatch-strategies|control-commands)/.test(call))).toEqual([])
})

test('三步原生工作流保留多类型绑定，编辑使试算与草稿收据失效', async ({ page }) => {
  const original = strategyView()
  original.draft.jdm_content = buildGenericDecisionTableJdm()
  const api = await installApi(page, original, [
    ...entities,
    { ...entities[0], id: 'mode', display_name: '运行模式', data_type: 'STRING', unit: null },
    { ...entities[1], id: 'fan', display_name: '风机启停', data_type: 'BOOL', unit: null },
    { ...entities[1], id: 'uncontrolled', display_name: '无控制合同', control_eligible: false },
  ])
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  const inputs = page.getByRole('region', { name: '1. 选择 L2 输入' })
  const table = page.getByRole('region', { name: '2. 编辑原生 JDM 决策表' })
  const outputs = page.getByRole('region', { name: '3. 绑定可控 L2 输出' })
  await expect(table).toBeVisible()
  await openBindingDialog(page, 'INPUT')
  await expect(inputs).toBeVisible({ timeout: 3000 })
  await inputs.getByRole('button', { name: '添加输入' }).click()
  await inputs.getByRole('button', { name: '添加输入' }).click()
  await page.getByLabel('输入 2 实体').selectOption('mode')
  await applyBindingDialog(page)
  await openBindingDialog(page, 'OUTPUT')
  await outputs.getByRole('button', { name: '添加输出' }).click()
  await outputs.getByRole('button', { name: '添加输出' }).click()
  await expect(page.getByLabel('输出 1 实体')).not.toContainText('无控制合同')
  await applyBindingDialog(page)
  await table.getByRole('button', { name: '添加可选示例列' }).click()
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByTestId('strategy-draft-receipt')).toContainText('draft-1')
  expect(api.savedDrafts[0].bindings.map((binding: any) => binding.expected_data_type)).toEqual(['FLOAT', 'STRING', 'FLOAT', 'BOOL'])
  expect(api.savedDrafts[0].jdm_content.nodes[1].content.inputs.map((column: any) => column.field)).toEqual(['site_local_minute', 'soc'])
  await page.getByRole('button', { name: '试算', exact: true }).click()
  await expect(page.getByTestId('strategy-simulation')).toContainText('未下发')
  await openBindingDialog(page, 'INPUT')
  await page.getByLabel('输入 1 别名').fill('temperature')
  await applyBindingDialog(page)
  await expect(page.getByTestId('strategy-simulation')).not.toBeVisible()
  await expect(page.getByTestId('strategy-draft-receipt')).not.toBeVisible()
  await expect(page.getByRole('region', { name: '策略状态' })).toContainText('未保存修改')
  await expect(page.getByRole('button', { name: '启用', exact: true })).toBeDisabled()
  expect(api.calls.some((call) => /control-commands|\/enable/.test(call))).toBe(false)
  for (const size of [{ width: 1280, height: 800 }, { width: 1024, height: 768 }]) {
    await page.setViewportSize(size)
    await page.getByLabel('策略名称').scrollIntoViewIfNeeded()
    await page.screenshot({ path: test.info().outputPath(`native-workflow-${size.width}.png`), fullPage: true })
    await table.getByRole('heading', { name: '2. 编辑原生 JDM 决策表' }).scrollIntoViewIfNeeded()
    await page.screenshot({ path: test.info().outputPath(`native-table-${size.width}.png`), fullPage: true })
    await openBindingDialog(page, 'OUTPUT')
    await page.screenshot({ path: test.info().outputPath(`native-outputs-${size.width}.png`), fullPage: true })
    await page.getByRole('button', { name: '取消', exact: true }).click()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  }
})

test('已保存的完整 JDM 改名保存不丢规则、触发或额外绑定', async ({ page }) => {
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  const original = strategyView()
  original.draft.trigger_kind = 'DATA_CHANGE'
  original.draft.site_timezone = 'Europe/Berlin'
  original.draft.jdm_content = {
    nodes: [
      { id: 'input', type: 'inputNode', name: 'Input' },
      { id: 'decide', type: 'expressionNode', name: '完整决策', content: { expressions: [{ id: 'target', key: 'target', value: 'soc > 80 && temperature < 40 ? 20 : 0' }] } },
      { id: 'output', type: 'outputNode', name: 'Output' },
    ],
    edges: [
      { id: 'i-d', sourceId: 'input', targetId: 'decide', type: 'edge' },
      { id: 'd-o', sourceId: 'decide', targetId: 'output', type: 'edge' },
    ],
  }
  original.draft.bindings = [
    { direction: 'INPUT', binding_key: 'soc', ordinal: 0, entity_instance_id: 'entity-soc', expected_data_type: 'FLOAT', unit: '%', freshness_seconds: 30 },
    { direction: 'INPUT', binding_key: 'temperature', ordinal: 1, entity_instance_id: 'entity-temperature', expected_data_type: 'FLOAT', unit: '°C', freshness_seconds: 45 },
    { direction: 'OUTPUT', binding_key: 'power-target', ordinal: 0, entity_instance_id: 'entity-limit', expected_data_type: 'FLOAT', unit: 'kW', freshness_seconds: 30 },
    { direction: 'OUTPUT', binding_key: 'run-mode', ordinal: 1, entity_instance_id: 'entity-mode', expected_data_type: 'INT', unit: null, freshness_seconds: 60 },
  ]
  const expectedRevision = structuredClone(original.draft)
  const api = await installApi(page, original, [...entities,
    { ...entities[0], id: 'entity-temperature', display_name: '温度', unit: '°C' },
    { ...entities[1], id: 'entity-mode', display_name: '模式', data_type: 'INT', unit: null },
  ])
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await expect(page.getByLabel('策略名称')).toHaveValue(original.name)
  await expect.soft(page.getByLabel('时段 1 功率目标')).not.toBeVisible()
  await openBindingDialog(page, 'INPUT')
  await expect(page.getByLabel('输入 2 实体')).toHaveValue('entity-temperature')
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await openBindingDialog(page, 'OUTPUT')
  await expect(page.getByLabel('输出 2 实体')).toHaveValue('entity-mode')
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await expect(page.getByText(/不是可无损往返的唯一决策表/)).toBeVisible()
  await page.getByRole('button', { name: '打开完整规则图', exact: true }).click()
  await expect(page.getByText('完整决策', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: '收起完整规则图', exact: true }).click()
  await page.getByLabel('策略名称').fill('只修改显示名称')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByTestId('dispatch-strategy-page').getByRole('status')).toContainText('草稿已保存')
  expect(api.savedDrafts).toHaveLength(1)
  const submitted = api.savedDrafts[0]
  expect(submitted.name).toBe('只修改显示名称')
  expect.soft(submitted.jdm_content).toEqual(expectedRevision.jdm_content)
  expect.soft(submitted.trigger_kind).toBe('DATA_CHANGE')
  expect.soft(submitted.site_timezone).toBe('Europe/Berlin')
  expect.soft(submitted.bindings).toEqual(expectedRevision.bindings)
  expect(pageErrors).toEqual([])
})

test('非标准 JDM 的旧缺失单位可显式保存并刷新到当前实体合同', async ({ page }) => {
  const original = strategyView()
  original.draft.base_configuration_revision = 6
  original.draft.trigger_kind = 'DATA_CHANGE'
  original.draft.jdm_content = {
    nodes: [
      { id: 'input', type: 'inputNode', name: 'Input' },
      { id: 'custom', type: 'expressionNode', name: '自定义完整决策', content: { expressions: [] } },
      { id: 'output', type: 'outputNode', name: 'Output' },
    ],
    edges: [
      { id: 'input-custom', sourceId: 'input', targetId: 'custom', type: 'edge' },
      { id: 'custom-output', sourceId: 'custom', targetId: 'output', type: 'edge' },
    ],
  }
  original.draft.bindings = [
    { direction: 'INPUT', binding_key: 'soc', ordinal: 0, entity_instance_id: 'entity-soc', expected_data_type: 'FLOAT', unit: null, freshness_seconds: 30 },
    { direction: 'INPUT', binding_key: 'reserve-soc', ordinal: 1, entity_instance_id: 'entity-soc', expected_data_type: 'FLOAT', unit: '%', freshness_seconds: 45 },
    { direction: 'OUTPUT', binding_key: 'power-target', ordinal: 0, entity_instance_id: 'entity-limit', expected_data_type: 'FLOAT', unit: null, freshness_seconds: 30 },
  ]
  const originalGraph = structuredClone(original.draft.jdm_content)
  const originalBindings = structuredClone(original.draft.bindings)
  const api = await installApi(page, original, entities, 7)
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await openBindingDialog(page, 'INPUT')
  await expect(page.getByLabel('输入 1 实体')).toHaveValue('entity-soc')
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await openBindingDialog(page, 'OUTPUT')
  await expect(page.getByLabel('输出 1 实体')).toHaveValue('entity-limit')
  await page.getByRole('button', { name: '取消', exact: true }).click()

  await page.getByRole('button', { name: '保存草稿', exact: true }).click()

  await expect(page.getByTestId('dispatch-strategy-page').getByRole('status')).toContainText('草稿已保存')
  expect(api.savedDrafts).toHaveLength(1)
  expect(api.savedDrafts[0].base_configuration_revision).toBe(6)
  expect(api.savedDrafts[0].jdm_content).toEqual(originalGraph)
  expect(api.savedDrafts[0].bindings).toEqual(originalBindings)
  expect(api.getStrategy().draft.base_configuration_revision).toBe(7)
  expect(api.getStrategy().draft.bindings.map((item: any) => item.unit)).toEqual(['%', '%', 'kW'])
})

test('运行事件默认10条可切20条，游标翻页不覆盖未保存草稿', async ({ page }) => {
  const api = await installApi(page, publishedStrategy())
  api.setEvents(Array.from({ length: 21 }, (_, index) => ({ id: `event-${index}`, occurred_at: now, event_kind: `RULE_${index}`, trigger_kind: 'FIXED_TICK', trigger_key: `tick:${index}`, frame_sequence: index, configuration_revision: 7, snapshot_evidence: {}, decision: null, intent_summary: [], control_command_id: null, control_status: null, reason_code: null })))
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  const region = page.getByRole('region', { name: '4. 关键事件与控制回读' })
  await expect(region.locator('tbody tr')).toHaveCount(10)
  await page.getByLabel('策略名称').fill('本地未保存')
  await region.getByRole('button', { name: '下一页' }).click()
  await expect(region.getByRole('cell', { name: 'RULE_10', exact: true })).toBeVisible()
  await expect(page.getByLabel('策略名称')).toHaveValue('本地未保存')
  await page.getByLabel('事件每页条数').selectOption('20')
  await expect(region.locator('tbody tr')).toHaveCount(20)
  await expect(region.getByRole('cell', { name: 'RULE_0', exact: true })).toBeVisible()
  expect(api.savedDrafts).toHaveLength(0)
})

test('原生策略显式选择正式触发方式，不改写同一 JDM 图', async ({ page }) => {
  const original = publishedStrategy()
  original.published_revision.trigger_kind = 'DATA_CHANGE'
  const graph = structuredClone(original.published_revision.jdm_content)
  const api = await installApi(page, original)
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await page.getByLabel('触发方式').selectOption('FIXED_TICK')
  await expect(page.getByRole('button', { name: '启用', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByTestId('strategy-draft-receipt')).toBeVisible()
  expect(api.savedDrafts[0].trigger_kind).toBe('FIXED_TICK')
  expect(api.savedDrafts[0].jdm_content).toEqual(graph)
})

test('策略目录读取失败显示真实错误而不是伪装成尚无策略', async ({ page }) => {
  await installApi(page)
  await page.route('**/api/v1/dispatch-strategies', (route) => route.fulfill({
    status: 403, contentType: 'application/json', body: JSON.stringify({ detail: { code: 'FORBIDDEN', message: '没有策略读取权限' } }),
  }))
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await expect(page.getByTestId('dispatch-strategy-page').getByRole('alert')).toContainText(/权限|授权/, { timeout: 3000 })
})

test('原生规则行编辑直接保存 action_id 与强类型目标到唯一 JDM', async ({ page }) => {
  const original = strategyView()
  original.draft.jdm_content = buildGenericDecisionTableJdm()
  const api = await installApi(page, original)
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await configureNativeControl(page)
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByTestId('strategy-draft-receipt')).toBeVisible()
  const content = api.savedDrafts[0].jdm_content.nodes[1].content
  expect(content.rules).toHaveLength(1)
  expect(content.rules[0]).toMatchObject({ action_id: '"power-target"', target: '0.5' })
  expect(content.hitPolicy).toBe('collect')
  expect(content.outputPath).toBe('intents')
})

for (const fullGraph of [false, true]) test(`原生${fullGraph ? '完整图' : '单表'}非文本新增行快速提交等待原生确认`, async ({ page }, testInfo) => {
  const original = publishedStrategy()
  const graph = original.published_revision.jdm_content
  graph.metadata = { vendorGraph: true }
  graph.nodes[1].vendorNode = 'keep'
  graph.nodes[1].content.rules[0].vendorEvidence = { revision: 9 }
  const count = graph.nodes[1].content.rules.length
  const api = await installApi(page, original)
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await expect(page.getByTestId('native-decision-table')).toBeVisible()
  if (fullGraph) {
    await page.getByRole('button', { name: '打开完整规则图' }).click()
    await page.locator('.react-flow__node').filter({ hasText: graph.nodes[1].name }).getByRole('button', { name: 'Edit Table' }).click()
  }
  const add = page.getByRole('button', { name: /Add row$/ })
  await expect(add).toBeVisible()
  await page.clock.install()
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000))
  await add.evaluate((button: HTMLButtonElement) => button.click())
  try {
    for (const name of ['保存草稿', '试算', '发布']) await expect(page.getByRole('button', { name, exact: true })).toBeDisabled({ timeout: 1000 })
  } catch (error) { await page.clock.resume(); throw error }
  expect(api.savedDrafts).toHaveLength(0)
  await page.clock.runFor(500)
  await page.clock.resume()
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect.poll(() => api.savedDrafts.length).toBe(1)
  const saved = api.savedDrafts[0].jdm_content
  expect(saved.nodes[1].content.rules).toHaveLength(count + 1)
  expect(saved.nodes[1].content.rules[0].vendorEvidence).toEqual({ revision: 9 })
  expect(saved.nodes[1].vendorNode).toBe('keep')
  expect(saved.metadata).toEqual({ vendorGraph: true })
  await add.scrollIntoViewIfNeeded()
  await page.screenshot({ path: testInfo.outputPath('native-sync-round-trip.png'), fullPage: true })
})

for (const fullGraph of [false, true]) test(`原生${fullGraph ? '完整图' : '单表'}增删列与删除行在快速提交前等待回调，删除内容不复活`, async ({ page }) => {
  const original = publishedStrategy()
  original.published_revision.jdm_content.nodes[1].content.rules[1].vendorEvidence = { keep: true }
  const api = await installApi(page, original)
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  if (fullGraph) {
    await page.getByRole('button', { name: '打开完整规则图' }).click()
    await page.getByRole('button', { name: 'Edit Table', exact: true }).click()
  }
  const table = page.getByTestId(fullGraph ? 'native-decision-graph' : 'native-decision-table')
  await table.locator('.head-cell').filter({ hasText: /^Inputs$/ }).getByRole('button').last().click()
  await page.getByPlaceholder('Field label').fill('Added condition')
  await page.clock.install()
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000))
  await page.getByRole('button', { name: 'Create', exact: true }).evaluate((button: HTMLButtonElement) => button.click())
  try { await expect(page.getByRole('button', { name: '保存草稿', exact: true })).toBeDisabled({ timeout: 1000 }) }
  catch (error) { await page.clock.resume(); throw error }
  await page.clock.runFor(500)
  await page.clock.resume()
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect.poll(() => api.savedDrafts.length).toBe(1)
  const added = api.savedDrafts[0].jdm_content.nodes[1].content.inputs.find((column: any) => column.name === 'Added condition')
  expect(added).toBeTruthy()
  await expect(page.getByTestId('strategy-draft-receipt')).toBeVisible()
  await table.locator('.head-cell').filter({ hasText: 'Added condition' }).locator('.grl-field-edit').click()
  const removeColumn = page.locator('.grl-field-edit__footer button').first()
  await removeColumn.click()
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000))
  await removeColumn.evaluate((button: HTMLButtonElement) => button.click())
  try { await expect(page.getByRole('button', { name: '发布', exact: true })).toBeDisabled({ timeout: 1000 }) }
  catch (error) { await page.clock.resume(); throw error }
  await page.clock.runFor(500)
  await page.clock.resume()
  await table.locator('.grl-dt__cell__input').first().click({ button: 'right' })
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000))
  await page.getByRole('menuitem', { name: 'Remove row' }).evaluate((item: HTMLElement) => item.click())
  try { await expect(page.getByRole('button', { name: '试算', exact: true })).toBeDisabled({ timeout: 1000 }) }
  catch (error) { await page.clock.resume(); throw error }
  await page.clock.runFor(500)
  await page.clock.resume()
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect.poll(() => api.savedDrafts.length).toBe(2)
  const saved = api.savedDrafts[1].jdm_content.nodes[1].content
  expect(saved.inputs.some((column: any) => column.id === added.id)).toBe(false)
  expect(saved.rules).toHaveLength(original.published_revision.jdm_content.nodes[1].content.rules.length - 1)
  expect(saved.rules[0].vendorEvidence).toEqual({ keep: true })
})

test('完整图命中策略切换后快速保存使用最新原生图', async ({ page }) => {
  const original = publishedStrategy()
  original.published_revision.jdm_content.nodes[1].content.hitPolicy = 'collect'
  const api = await installApi(page, original)
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await page.getByRole('button', { name: '打开完整规则图' }).click()
  await page.locator('.react-flow__node').filter({ hasText: original.published_revision.jdm_content.nodes[1].name }).getByRole('button', { name: 'Settings' }).click()
  await page.clock.install()
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000))
  await page.getByRole('radio', { name: 'First', exact: true }).evaluate((input: HTMLInputElement) => input.click())
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect.poll(() => api.savedDrafts.length).toBe(1)
  await page.clock.resume()
  expect(api.savedDrafts[0].jdm_content.nodes[1].content.hitPolicy).toBe('first')
})

test('取消原生菜单后可明确确认放弃未同步编辑恢复提交', async ({ page }) => {
  const original = publishedStrategy()
  const api = await installApi(page, original)
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await page.getByTestId('native-decision-table').locator('.head-cell').filter({ hasText: /^Inputs$/ }).getByRole('button').last().click()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(page.getByRole('button', { name: '保存草稿', exact: true })).toBeDisabled({ timeout: 1000 })
  page.once('dialog', (dialog) => dialog.dismiss())
  await page.getByRole('button', { name: '放弃未确认编辑', exact: true }).click()
  await expect(page.getByRole('button', { name: '保存草稿', exact: true })).toBeDisabled()
  page.once('dialog', (dialog) => dialog.accept())
  await page.getByRole('button', { name: '放弃未确认编辑', exact: true }).click()
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect.poll(() => api.savedDrafts.length).toBe(1)
  expect(api.savedDrafts[0].jdm_content).toEqual(original.published_revision.jdm_content)
})

test('完整图同节点改名不能确认尚未回调的表内容，三个提交入口等待C1', async ({ page }, testInfo) => {
  const original = publishedStrategy()
  const count = original.published_revision.jdm_content.nodes[1].content.rules.length
  const api = await installApi(page, original)
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await page.getByRole('button', { name: '打开完整规则图' }).click()
  const node = page.locator(`.react-flow__node[data-id="${original.published_revision.jdm_content.nodes[1].id}"]`)
  await node.getByRole('button', { name: 'Edit Table', exact: true }).click()
  await page.clock.install()
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000))
  try {
    await page.getByRole('button', { name: /Add row$/ }).evaluate((button: HTMLButtonElement) => button.click())
    // Attempt a real native tab switch and same-node rename while C1 is still
    // inside the table debounce. A safe editor may reject the navigation itself.
    await page.getByRole('tab').filter({ hasText: 'Graph' }).evaluate((tab: HTMLElement) => tab.click())
    await node.locator('.grl-text-edit__text').first().evaluate((label: HTMLElement) => label.click())
    const nameInput = node.locator('input.grl-text-edit__input')
    if (await nameInput.count()) {
      await nameInput.fill('same-node-metadata-C0')
      await nameInput.press('Tab')
    }
    for (const name of ['保存草稿', '试算', '发布']) {
      const submit = page.getByRole('button', { name, exact: true })
      await expect(submit).toBeDisabled({ timeout: 1000 })
      await submit.evaluate((button: HTMLButtonElement) => button.click())
    }
    expect(api.savedDrafts).toHaveLength(0)
    expect(api.calls.filter((call) => /^(PUT|POST).*\/(draft|simulate|publish)$/.test(call))).toEqual([])
    await page.getByRole('button', { name: '保存草稿', exact: true }).scrollIntoViewIfNeeded()
    await page.screenshot({ path: testInfo.outputPath('native-content-pending.png'), fullPage: true })
    await page.clock.runFor(500)
  } finally { await page.clock.resume() }
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByTestId('strategy-draft-receipt')).toBeVisible()
  expect(api.savedDrafts[0].jdm_content.nodes[1].content.rules).toHaveLength(count + 1)
  expect(api.savedDrafts[0].jdm_content.nodes[1].name).toBe(original.published_revision.jdm_content.nodes[1].name)
  // Navigation and metadata editing work again after the native C1 commit.
  await page.getByRole('tab').filter({ hasText: 'Graph' }).click()
  await node.locator('.grl-text-edit__text').first().evaluate((label: HTMLElement) => label.click())
  await node.locator('input.grl-text-edit__input').fill('metadata-after-C1')
  await page.keyboard.press('Tab')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect.poll(() => api.savedDrafts.length).toBe(2)
  expect(api.savedDrafts[1].jdm_content.nodes[1]).toMatchObject({ name: 'metadata-after-C1' })
  expect(api.savedDrafts[1].jdm_content.nodes[1].content.rules).toHaveLength(count + 1)
})

test('已有时段 JDM 在原生表改名保存仍保留条件、公式与触发原义', async ({ page }) => {
  const original = publishedStrategy()
  original.published_revision.trigger_kind = 'DATA_CHANGE'
  original.published_revision.jdm_content.nodes[1].content.rules[0].site_local_minute = '[1320..1440)'
  original.published_revision.jdm_content.metadata = { external: 'keep' }
  const before = structuredClone(original.published_revision)
  const api = await installApi(page, original)
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await expect(page.getByTestId('native-decision-table')).toBeVisible()
  await expect(page.getByLabel('时段 1 功率目标')).not.toBeVisible()
  await page.getByLabel('策略名称').fill('保留时段原义')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByTestId('strategy-draft-receipt')).toBeVisible()
  expect(api.savedDrafts[0].jdm_content).toEqual(before.jdm_content)
  expect(api.savedDrafts[0].bindings).toEqual(before.bindings)
  expect(api.savedDrafts[0].trigger_kind).toBe('DATA_CHANGE')
})

test('不合格 L2 绑定不能保存且不自动改选；明确重新绑定后恢复', async ({ page }) => {
  const original = publishedStrategy()
  const api = await installApi(page, original, [
    { ...entities[0], confirmed: false }, entities[1],
    { ...entities[0], id: 'confirmed-input', definition_id: 'cabinet.temperature', unit: '°C' },
  ])
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByTestId('dispatch-strategy-page').getByRole('alert')).toContainText('不再可读')
  expect(api.savedDrafts).toHaveLength(0)
  await openBindingDialog(page, 'INPUT')
  await expect(page.getByLabel('输入 1 实体')).toHaveValue('entity-soc')
  await page.getByLabel('输入 1 实体').selectOption('confirmed-input')
  await applyBindingDialog(page)
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByTestId('strategy-draft-receipt')).toBeVisible()
  expect(api.savedDrafts[0].bindings[0]).toMatchObject({ entity_instance_id: 'confirmed-input', unit: '°C' })
})

test('空候选不提供伪造实体或控制输出', async ({ page }) => {
  const api = await installApi(page, strategyView(), [])
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await openBindingDialog(page, 'INPUT')
  await page.getByRole('button', { name: '添加输入' }).click()
  await expect(page.getByTestId('dispatch-strategy-page').getByRole('alert')).toContainText('没有更多可读')
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await openBindingDialog(page, 'OUTPUT')
  await page.getByRole('button', { name: '添加输出' }).click()
  await expect(page.getByTestId('dispatch-strategy-page').getByRole('alert')).toContainText('没有更多明确具备控制资格')
  await expect(page.getByText(/请先配置正式 L2 控制合同/)).toBeVisible()
  expect(api.savedDrafts).toHaveLength(0)
})

test('发布与启用明确分开；故障锁解除不恢复运行，事件受理不冒充回读成功', async ({ page }) => {
  const api = await installApi(page, publishedStrategy())
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await page.getByLabel('策略名称').fill('新的调度修订')
  await expect(page.getByRole('button', { name: '启用', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '发布', exact: true }).click()
  await expect(page.getByTestId('dispatch-strategy-page').getByRole('status')).toContainText('已发布为不可变版本')
  expect(api.calls.some((call) => call.endsWith('/enable'))).toBe(false)
  await page.getByRole('button', { name: '启用', exact: true }).click()
  await expect(page.getByRole('region', { name: '策略状态' })).toContainText('已启用')
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(page.getByRole('cell', { name: /等待回读/ })).toBeVisible()
  await page.getByRole('button', { name: '查看控制回读 command-1' }).click()
  await expect(page.getByTestId('strategy-control-evidence')).toContainText('dispatched')
  await expect(page.getByTestId('strategy-control-evidence')).not.toContainText('回读确认到位')
  await page.route('**/api/v1/control-commands/command-1', (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify({ id: 'command-1', status: 'readback_confirmed', code: 'READBACK_CONFIRMED', source_type: 'strategy', readback_evidence: { frame_sequence: 43, value: 80, observed_at: now } }),
  }))
  await page.getByRole('button', { name: '查看控制回读 command-1' }).click()
  await expect(page.getByTestId('strategy-control-evidence')).toContainText('回读确认到位')
  await expect(page.getByTestId('strategy-control-evidence')).toContainText('43')
  await page.getByRole('button', { name: '停用', exact: true }).click()
  await expect(page.getByRole('region', { name: '策略状态' })).toContainText('已停用')
  api.getStrategy().runtime_health = 'FAILED'
  api.getStrategy().failure_code = 'CONTROL_COMMAND_FAILED'
  await page.reload()
  await openEngineeringPage(page, '调度策略')
  await expect(page.getByRole('button', { name: '启用', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '清除故障锁' }).click()
  await expect(page.getByTestId('dispatch-strategy-page').getByRole('status')).toContainText('仍保持停用')
  expect(api.getStrategy().enabled).toBe(false)
  expect(api.calls).toContain('POST /dispatch-strategies/strategy-1/failure-latch/clear')
  expect(api.calls.filter((call) => call.includes('control-commands'))).toEqual(['GET /control-commands/command-1'])
})

function publishedStrategy() {
  const original = strategyView()
  original.draft = null
  original.published_revision = revision('published-1', 'PUBLISHED', { bindings: [
    { direction: 'INPUT', binding_key: 'soc', ordinal: 0, entity_instance_id: 'entity-soc', expected_data_type: 'FLOAT', unit: '%', freshness_seconds: 10 },
    { direction: 'OUTPUT', binding_key: 'power-target', ordinal: 0, entity_instance_id: 'entity-limit', expected_data_type: 'FLOAT', unit: 'kW', freshness_seconds: 10 },
  ] })
  return original
}

test('策略卡片用当前 committed L2 代替上次决策快照冒充回读', async ({ page }) => {
  const original = publishedStrategy()
  original.last_desired = { 'power-target': 120 }
  original.last_actual = { 'power-target': 121 }
  await installApi(page, original)

  await page.goto('/')
  await openEngineeringPage(page, '调度策略')

  const card = page.getByLabel('策略列表')
  await expect(card).toContainText('当前 L2 156.8')
  await expect(card).not.toContainText('回读')
  await expect(card).not.toContainText('121')
})

test('已发布策略直接试算不保存草稿、不改变发布状态', async ({ page }) => {
  const api = await installApi(page, publishedStrategy())
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await expect(page.getByLabel('策略名称')).toBeVisible()
  const request = page.waitForRequest((item) => item.url().endsWith('/simulate'))
  await page.getByRole('button', { name: '试算', exact: true }).click()
  expect((await request).postDataJSON()).toEqual({ revision_id: 'published-1', expected_digest: 'c'.repeat(64) })
  await expect(page.getByTestId('strategy-simulation')).toContainText('帧 42')
  expect(api.savedDrafts).toHaveLength(0)
  await expect(page.getByRole('region', { name: '策略状态' })).not.toContainText('有未发布修改')
  // The runtime shell obtains read-only stream tickets before entering engineering.
  expect(api.calls.filter((call) => call.startsWith('POST ') && call !== 'POST /auth/ws-ticket'))
    .toEqual(['POST /dispatch-strategies/strategy-1/simulate'])
})

test('试算被数据超时阻断时明确解释原因且不显示无需控制', async ({ page }) => {
  await installApi(page, publishedStrategy())
  await page.route('**/dispatch-strategies/strategy-1/simulate', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      status: 'BLOCKED', reason_code: 'L2_INPUT_STALE', frame_sequence: 42, configuration_revision: 7,
      snapshot: {
        soc: { entity_instance_id: 'entity-soc', value: 50, data_type: 'FLOAT', unit: '%', quality: 'GOOD', observed_at: now, frame_sequence: 41, configuration_revision: 7 },
      },
      engine_inputs: {}, matched_rules: [], decision: null, proposed_intents: [],
    }),
  }))
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await expect(page.getByLabel('策略名称')).toBeVisible()
  await page.getByRole('button', { name: '试算', exact: true }).click()
  const result = page.getByTestId('strategy-simulation')
  await expect(result).toContainText('超时', { timeout: 3000 })
  await expect(result).toContainText('未执行计算')
  await expect(result).not.toContainText('无需控制')
  await expect(result).toContainText('50')
  await expect(result).toContainText('%')
  await expect(result).toContainText('2026')
  await page.screenshot({ path: test.info().outputPath('blocked-simulation.png'), fullPage: true })
})

test('修改后试算使用新草稿，非法输入不能复用旧结果', async ({ page }) => {
  const api = await installApi(page, publishedStrategy())
  await page.goto('/')
  await openEngineeringPage(page, '调度策略')
  await expect(page.getByLabel('策略名称')).toBeVisible()
  await openBindingDialog(page, 'INPUT')
  await page.getByLabel('输入 1 别名').fill('new_input')
  await applyBindingDialog(page)
  const request = page.waitForRequest((item) => item.url().endsWith('/simulate'))
  await page.getByRole('button', { name: '试算', exact: true }).click()
  expect((await request).postDataJSON()).toEqual({ revision_id: 'draft-1', expected_digest: 'b'.repeat(64) })
  await expect(page.getByTestId('strategy-simulation')).toBeVisible()
  expect(api.savedDrafts).toHaveLength(1)
  expect(api.savedDrafts[0].bindings[0].binding_key).toBe('new_input')
  await page.getByRole('button', { name: '试算', exact: true }).click()
  await expect(page.getByTestId('strategy-simulation')).toBeVisible()
  expect(api.savedDrafts).toHaveLength(1)
  const requestsBefore = api.calls.filter((call) => call.endsWith('/simulate')).length
  await openBindingDialog(page, 'INPUT')
  await page.getByLabel('输入 1 别名').fill('')
  await applyBindingDialog(page)
  await page.getByRole('button', { name: '试算', exact: true }).click()
  await expect(page.getByTestId('dispatch-strategy-page').getByRole('alert')).toContainText('别名不能为空')
  await expect(page.getByTestId('strategy-simulation')).not.toBeVisible()
  expect(api.savedDrafts).toHaveLength(1)
  expect(api.calls.filter((call) => call.endsWith('/simulate'))).toHaveLength(requestsBefore)
})

test.describe.serial('调度策略本机真实纵向验收', () => {
  test.describe.configure({ timeout: 240_000 })
  const backendPort = 19026
  const frontendPort = 4174
  const databaseName = `zizu_task8_${randomUUID().replaceAll('-', '')}_test`
  const password = `dispatch-${randomUUID()}`
  let database: Record<string, string>
  let backend: ChildProcess | undefined
  let frontend: ChildProcess | undefined

  test.beforeAll(async () => {
    database = localDatabaseEnvironment(process.env, databaseName)
    await createDisposablePostgresDatabase(databaseName)
    backend = await startLocalDispatchFixture(database, password, backendPort)
    frontend = await startPreviewForLocalFixture(backendPort, frontendPort)
  })

  test.afterAll(async () => {
    await stopProcess(frontend)
    await stopProcess(backend)
    await dropDisposablePostgresDatabase(database)
  })

  test('浏览器经真实 JDM、提交 L2 与统一控制完成一次策略生命周期', async ({ browser, request }) => {
    test.setTimeout(180_000)
    const protocolSoc = 50.5
    const consoleErrors: string[] = []
    const context = await browser.newContext({ baseURL: `http://127.0.0.1:${frontendPort}` })
    const page = await context.newPage()
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    page.on('pageerror', (error) => consoleErrors.push(error.message))

    await page.goto('/')
    await page.getByLabel('用户名').fill('local-e2e')
    await page.getByLabel('密码').fill(password)
    const loginResponse = page.waitForResponse((response) => {
      const url = new URL(response.url())
      return response.request().method() === 'POST'
        && url.pathname === '/api/v1/auth/login'
    })
    await page.getByRole('button', { name: '登录', exact: true }).click()
    expect((await loginResponse).status()).toBe(200)
    await expect.poll(() => page.evaluate(() => sessionStorage.getItem('zizu.auth.session.v1') !== null)).toBe(true)
    const workbenchToken = await page.evaluate(() => {
      const raw = sessionStorage.getItem('zizu.auth.session.v1')
      if (!raw) throw new Error('authenticated browser session is missing')
      const { accessToken } = JSON.parse(raw) as { accessToken: string }
      return accessToken
    })
    const workbench = await request.get(`http://127.0.0.1:${backendPort}/api/v1/ems-workbench`, {
      headers: { Authorization: `Bearer ${workbenchToken}` },
    })
    expect(workbench.status()).toBe(200)
    const workbenchPayload = await workbench.json()
    expect(workbenchPayload.kpis).toHaveLength(5)
    expect(workbenchPayload.kpis.map((kpi: { id: string }) => kpi.id)).toEqual([
      'site-power',
      'pv-power',
      'storage-power',
      'storage-soc',
      'charging-power',
    ])
    consoleErrors.length = 0
    await openEngineeringPage(page, '调度策略')
    const strategyId = await createStrategyDraft(page)
    await configureNativeControl(page)
    const freshSnapshot = await request.post(`http://127.0.0.1:${backendPort}/protocol-simulator/neuron`, {
      data: { node: 'strategy-test', group: 'group0', values: { soc: protocolSoc, limit: 156.8 } },
    })
    if (!freshSnapshot.ok()) {
      throw new Error(`local protocol snapshot failed ${freshSnapshot.status()}: ${await freshSnapshot.text()}\n${localFixtureOutput.slice(-8000)}`)
    }
    await page.getByRole('button', { name: '试算', exact: true }).click()
    await expect(page.getByTestId('strategy-simulation')).toContainText('快照')
    await expect(page.getByTestId('strategy-simulation')).toContainText(/命中行|other-time/)
    await expect(page.getByTestId('strategy-simulation')).toContainText('power-target=0.5')
    await page.getByRole('button', { name: '发布', exact: true }).click()
    await expect(page.getByText('已发布为不可变版本；确认后可启用。')).toBeVisible()
    await page.getByRole('button', { name: '启用', exact: true }).click()
    await expect(page.getByRole('region', { name: '策略状态' })).toContainText('已启用')
    await expect(page.getByRole('region', { name: '策略状态' })).toContainText('就绪')

    // This is the test-only protocol boundary. It creates a committed L2 frame;
    // it never manufactures a strategy event, intent, command, or readback state.
    const first = await request.post(`http://127.0.0.1:${backendPort}/protocol-simulator/neuron`, {
      data: { node: 'strategy-test', group: 'group0', values: { soc: protocolSoc, limit: 1.5 } },
    })
    expect(first.ok()).toBeTruthy()
    await expect.poll(async () => (await request.get(`http://127.0.0.1:${backendPort}/test-fixture/state?strategy_id=${strategyId}`)).json(), {
      timeout: 40_000,
    }).toMatchObject({ strategy_id: strategyId, strategy_events: 2, intents: 1, commands: 1, dispatched: 1, device_submissions: 1, events: expect.any(Array) })
    const firstState = await (await request.get(`http://127.0.0.1:${backendPort}/test-fixture/state?strategy_id=${strategyId}`)).json()
    expect(firstState.events).toHaveLength(2)
    expect(firstState.events.map((event: { event_kind: string }) => event.event_kind).sort()).toEqual(['DECISION_CHANGED', 'INTENT_CREATED'])
    expect(new Set(firstState.events.map((event: { trigger_key: string }) => event.trigger_key)).size).toBe(1)
    expect(new Set(firstState.events.map((event: { snapshot_evidence: object }) => JSON.stringify(event.snapshot_evidence))).size).toBe(1)

    const second = await request.post(`http://127.0.0.1:${backendPort}/protocol-simulator/neuron`, {
      data: { node: 'strategy-test', group: 'group0', values: { soc: protocolSoc, limit: 0.5 } },
    })
    expect(second.ok()).toBeTruthy()
    await expect.poll(async () => (await request.get(`http://127.0.0.1:${backendPort}/test-fixture/state?strategy_id=${strategyId}`)).json(), {
      timeout: 40_000,
    }).toMatchObject({ strategy_id: strategyId, strategy_events: 2, intents: 1, commands: 1, readback_confirmed: 1, device_submissions: 1, events: expect.any(Array) })
    const readbackState = await (await request.get(`http://127.0.0.1:${backendPort}/test-fixture/state?strategy_id=${strategyId}`)).json()
    expect(readbackState.events).toEqual(firstState.events)
    const sameMinute = await request.post(`http://127.0.0.1:${backendPort}/test-fixture/pump`)
    expect(sameMinute.ok()).toBeTruthy()
    const repeatedState = await (await request.get(`http://127.0.0.1:${backendPort}/test-fixture/state?strategy_id=${strategyId}`)).json()
    expect(repeatedState.clock).toBe(firstState.clock)
    expect(repeatedState.events).toEqual(firstState.events)
    expect(repeatedState).toMatchObject({ intents: 1, commands: 1, device_submissions: 1 })
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    await expect(page.getByRole('heading', { name: '4. 关键事件与控制回读' })).toBeVisible()
    await expect(page.getByText('INTENT_CREATED', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '停用', exact: true }).click()
    await expect(page.getByRole('region', { name: '策略状态' })).toContainText('已停用')
    const disabled = await request.post(`http://127.0.0.1:${backendPort}/protocol-simulator/neuron`, {
      data: { node: 'strategy-test', group: 'group0', values: { soc: protocolSoc, limit: 0.5 } },
    })
    expect(disabled.ok()).toBeTruthy()
    await expect.poll(async () => (await request.get(`http://127.0.0.1:${backendPort}/test-fixture/state?strategy_id=${strategyId}`)).json()).toMatchObject({
      strategy_id: strategyId, strategy_events: 2, intents: 1, commands: 1, enabled: false, device_submissions: 1,
    })
    expect(consoleErrors).toEqual([])
    await context.close()
  })

  test('适配器写入失败只提交一次、锁定并停用策略，后续同分钟 pump 不重发', async ({ browser, request }) => {
    const context = await browser.newContext({ baseURL: `http://127.0.0.1:${frontendPort}` })
    const page = await context.newPage()
    await page.goto('/')
    await page.getByLabel('用户名').fill('local-e2e')
    await page.getByLabel('密码').fill(password)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await openEngineeringPage(page, '调度策略')
    const strategyId = await createStrategyDraft(page)
    await configureNativeControl(page)
    const baseline = await request.post(`http://127.0.0.1:${backendPort}/protocol-simulator/neuron`, {
      data: { node: 'strategy-test', group: 'group0', values: { soc: 50.5, limit: 156.8 } },
    })
    expect(baseline.ok()).toBeTruthy()
    await page.getByRole('button', { name: '发布', exact: true }).click()
    await expect(page.getByText('已发布为不可变版本；确认后可启用。')).toBeVisible()
    await page.getByRole('button', { name: '启用', exact: true }).click()
    const armFailure = await request.post(`http://127.0.0.1:${backendPort}/test-fixture/adapter-failure`)
    expect(armFailure.ok()).toBeTruthy()
    const failedWrite = await request.post(`http://127.0.0.1:${backendPort}/protocol-simulator/neuron`, {
      data: { node: 'strategy-test', group: 'group0', values: { soc: 50.5, limit: 1.5 } },
    })
    expect(failedWrite.ok()).toBeTruthy()
    await expect.poll(async () => (await request.get(`http://127.0.0.1:${backendPort}/test-fixture/state?strategy_id=${strategyId}`)).json()).toMatchObject({
      strategy_id: strategyId, enabled: false, runtime_health: 'FAILED',
      intents: 1, commands: 1, device_submissions: 1,
      intent_statuses: ['FAILED'], command_statuses: ['failed'],
    })
    const failed = await (await request.get(`http://127.0.0.1:${backendPort}/test-fixture/state?strategy_id=${strategyId}`)).json()
    const repeated = await request.post(`http://127.0.0.1:${backendPort}/test-fixture/pump`)
    expect(repeated.ok()).toBeTruthy()
    const afterRepeat = await (await request.get(`http://127.0.0.1:${backendPort}/test-fixture/state?strategy_id=${strategyId}`)).json()
    expect(afterRepeat).toMatchObject({ enabled: false, runtime_health: 'FAILED', device_submissions: 1 })
    expect(afterRepeat.events).toEqual(failed.events)
    await context.close()
  })

  test('浏览器沿节点树、L0、L1、L2、告警到调度策略核验同一 L2 身份', async ({ browser, request }) => {
    const consoleErrors: string[] = []
    const context = await browser.newContext({ baseURL: `http://127.0.0.1:${frontendPort}` })
    const page = await context.newPage()
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    page.on('pageerror', (error) => consoleErrors.push(error.message))

    await page.goto('/')
    await page.getByLabel('用户名').fill('local-e2e')
    await page.getByLabel('密码').fill(password)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    consoleErrors.length = 0

    // The only deterministic boundary stays at the protocol edge.  The fresh
    // observation is committed through the real L0/L1/L2 outbox path before
    // the browser examines it.
    const fresh = await request.post(`http://127.0.0.1:${backendPort}/protocol-simulator/neuron`, {
      data: { node: 'strategy-test', group: 'group0', values: { soc: 50.5, limit: 0.5 } },
    })
    expect(fresh.ok()).toBeTruthy()

    await openEngineeringPage(page, '节点与数据')
    await page.getByPlaceholder('搜索节点...').fill('strategy-test')
    await page.getByTitle('strategy-test', { exact: true }).click()
    await expect(page.getByRole('region', { name: '原始数据' })).toBeVisible()
    await page.getByPlaceholder('搜索点位名称').fill('soc')
    await expect(page.getByRole('row').filter({ hasText: 'soc' })).toContainText('50.5')
    await expect(page.getByRole('row').filter({ hasText: 'soc' })).toContainText('正常')

    await page.getByRole('checkbox', { name: '选择 soc' }).check()
    const l1 = page.getByLabel('加工为实体')
    await expect(l1).toContainText('已选择 1 个原始点位')
    await l1.getByRole('button', { name: '加工为实体', exact: true }).click()
    const l1Editor = page.getByRole('dialog', { name: '新建标准实体', exact: true })
    await expect(l1Editor.getByLabel('加工方法')).toHaveValue('passthrough')
    await l1Editor.getByText('高级设置', { exact: true }).click()
    await expect(l1Editor).toContainText('业务标识')
    await l1Editor.getByRole('button', { name: '取消', exact: true }).click()

    await page.getByRole('button', { name: '点位加工', exact: true }).click()
    await expect(
      page.getByRole('region', { name: '数据来源与计算' }).getByText('已生效', { exact: true }),
    ).toBeVisible()

    await page.getByRole('button', { name: '标准实体', exact: true }).click()
    await expect(page.getByRole('heading', { name: '实体实时数据' })).toBeVisible()
    const socEntity = page.getByRole('button', { name: /PCS 品牌 A · bms\.soc/ })
    await expect(socEntity).toContainText('50.5')
    await socEntity.click()
    await expect(page.getByRole('region', { name: '实体来源' })).toContainText('soc')
    await page.getByRole('region', { name: '实体来源' }).getByText('技术详情', { exact: true }).click()
    await expect(page.getByRole('region', { name: '实体来源' })).toContainText('definition_id: bms.soc')
    await expect(page.getByRole('region', { name: '实体来源' })).toContainText('processing_revision_id:')

    await openEngineeringPage(page, '调度策略')
    const strategyId = await createStrategyDraft(page)
    await configureNativeControl(page)
    await page.getByRole('button', { name: '保存草稿', exact: true }).click()
    await expect(page.getByTestId('dispatch-strategy-page').getByRole('status')).toContainText('草稿已保存')

    await openEngineeringPage(page, '告警')
    await page.getByRole('button', { name: '告警规则', exact: true }).click()
    await page.getByPlaceholder('搜索实体名称、业务标识或节点').fill('bms.soc')
    const alarmEntity = page.locator('label').filter({ hasText: 'bms.soc' })
    await expect(alarmEntity).toBeVisible()
    await alarmEntity.getByRole('checkbox').check()
    await page.getByLabel('规则名称').fill('Task8 L2 identity')
    await page.getByLabel('故障名称').fill('Task8 SOC warning')
    await page.getByLabel('触发值').fill('50')
    await page.getByLabel('恢复值').fill('49')
    await page.getByLabel('试算值').fill('50.5')
    await page.getByRole('button', { name: '试算', exact: true }).click()
    await expect(page.getByText(/会触发警告告警/)).toBeVisible()
    await page.getByRole('button', { name: '生成发布预览', exact: true }).click()
    await expect(page.getByRole('button', { name: '确认发布', exact: true })).toBeVisible()
    await page.getByRole('button', { name: '确认发布', exact: true }).click()
    await expect(page.getByText(/已发布，统一配置版本/)).toBeVisible()
    const alarmRulesDialog = page.getByRole('dialog', { name: '告警规则配置', exact: true })
    await alarmRulesDialog.getByRole('button', { name: '关闭', exact: true }).click()
    await expect(alarmRulesDialog).toBeHidden()

    const alarmTrigger = await request.post(`http://127.0.0.1:${backendPort}/protocol-simulator/neuron`, {
      data: { node: 'strategy-test', group: 'group0', values: { soc: 50.5, limit: 0.5 } },
    })
    expect(alarmTrigger.ok(), `alarm protocol sample failed ${alarmTrigger.status()}: ${await alarmTrigger.text()}\n${localFixtureOutput.slice(-8000)}`).toBeTruthy()
    await page.getByRole('button', { name: '当前告警', exact: true }).click()
    await expect(page.getByText('Task8 SOC warning', { exact: true })).toBeVisible()

    const identity = await (await request.get(`http://127.0.0.1:${backendPort}/test-fixture/state?strategy_id=${strategyId}`)).json()
    expect(identity.strategy_id).toBe(strategyId)
    expect(identity.alarm_entity_ids).toEqual([identity.strategy_soc_entity_id])
    expect(consoleErrors).toEqual([])
    await context.close()
  })
})

async function createDisposablePostgresDatabase(name: string): Promise<Record<string, string>> {
  const database = localDatabaseEnvironment(process.env, name)
  await execFileAsync('C:\\veighna_studio\\python.exe', ['-c', 'import os, psycopg2; c=psycopg2.connect(host=os.environ["DB_HOST"],port=os.environ["DB_PORT"],user=os.environ["DB_USER"],password=os.environ["DB_PASSWORD"],dbname="postgres"); c.autocommit=True; cur=c.cursor(); cur.execute("CREATE DATABASE " + os.environ["DB_NAME"]); c.close(); d=psycopg2.connect(host=os.environ["DB_HOST"],port=os.environ["DB_PORT"],user=os.environ["DB_USER"],password=os.environ["DB_PASSWORD"],dbname=os.environ["DB_NAME"]); d.autocommit=True; d.cursor().execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")'], { env: { ...process.env, ...database } })
  return database
}

async function dropDisposablePostgresDatabase(database: Record<string, string>): Promise<void> {
  if (!database) return
  localDatabaseEnvironment(database, database.DB_NAME)
  await execFileAsync('C:\\veighna_studio\\python.exe', ['-c', 'import os, psycopg2; c=psycopg2.connect(host=os.environ["DB_HOST"],port=os.environ["DB_PORT"],user=os.environ["DB_USER"],password=os.environ["DB_PASSWORD"],dbname="postgres"); c.autocommit=True; cur=c.cursor(); cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (os.environ["DB_NAME"],)); cur.execute("DROP DATABASE IF EXISTS " + os.environ["DB_NAME"])'], { env: { ...process.env, ...database } })
}

async function startLocalDispatchFixture(database: Record<string, string>, localPassword: string, port: number): Promise<ChildProcess> {
  const root = path.resolve(process.cwd(), '..')
  return startUntilReady('C:\\veighna_studio\\python.exe', [path.join(root, 'backend', 'scripts', 'node_management_e2e_fixture.py'), 'local-dispatch-server', '--port', String(port)], {
    ...process.env, ...database, ZIZU_LOCAL_E2E_PASSWORD: localPassword,
    NEURON_PASSWORD: localPassword, NANOMQ_API_PASSWORD: localPassword, JWT_SECRET: localPassword,
    NEURON_API_URL: 'http://127.0.0.1:17994', NANOMQ_API_URL: 'http://127.0.0.1:18994',
    MQTT_HOST: '127.0.0.1', MQTT_PORT: '21994',
    DEPLOYMENT_MODE: 'development', AUTH_REQUIRE_HTTPS: 'false', ALLOW_INSECURE_DEV_SECRETS: 'false',
    PYTHONPATH: `C:\\Users\\chent\\AppData\\Local\\Temp\\zizu-v087-local-514b7ea4b65748a1b7673a8577fe3ef8\\python-packages;${path.join(root, 'backend')};${root}`,
  }, 'LOCAL_DISPATCH_FIXTURE')
}

async function startPreviewForLocalFixture(backendPort: number, port: number): Promise<ChildProcess> {
  return startUntilReady('npm.cmd', ['run', 'preview', '--', '--host', '127.0.0.1', '--port', String(port), '--strictPort'], {
    ...process.env, ZIZU_DEV_PROXY_TARGET: `http://127.0.0.1:${backendPort}`,
  }, 'Local:')
}

async function createStrategyDraft(page: Page): Promise<string> {
  const created = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST'
      && url.pathname === '/api/v1/dispatch-strategies'
  })
  await page.getByRole('button', { name: '新建通用策略' }).click()
  const strategyId = (await (await created).json()).id as string
  const loaded = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'GET'
      && url.pathname === `/api/v1/dispatch-strategies/${strategyId}`
  })
  await loaded
  await expect(page.getByTestId('native-decision-table')).toBeVisible()
  await expect(page.getByRole('button', { name: /01.*选择 L2 输入/ })).toBeVisible()
  return strategyId
}

async function configureNativeControl(page: Page) {
  await page.getByLabel('触发方式').selectOption('FIXED_TICK')
  await openBindingDialog(page, 'INPUT')
  await page.getByRole('button', { name: '添加输入' }).click()
  await page.getByLabel('输入 1 别名').fill('soc')
  await page.getByLabel('输入 1 实体').selectOption({ index: 1 })
  await applyBindingDialog(page)
  await openBindingDialog(page, 'OUTPUT')
  await page.getByRole('button', { name: '添加输出' }).click()
  await page.getByLabel('输出 1 别名').fill('power-target')
  await page.getByLabel('输出 1 实体').selectOption({ index: 1 })
  await applyBindingDialog(page)
  await page.getByTestId('native-decision-table').getByRole('button', { name: /Add row$/ }).click()
  const cells = page.getByTestId('native-decision-table').locator('.grl-dt__cell__input')
  await cells.nth(1).click()
  await page.keyboard.type('"power-target"')
  await page.keyboard.press('Tab')
  await cells.nth(2).click()
  await page.keyboard.type('0.5')
  await page.keyboard.press('Tab')
}

function startUntilReady(command: string, args: string[], env: NodeJS.ProcessEnv, ready: string): Promise<ChildProcess> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: process.cwd(), env, windowsHide: true, shell: command.endsWith('.cmd') })
    let output = ''
    const timer = setTimeout(() => reject(new Error(`local fixture did not become ready: ${output}`)), 60_000)
    child.stdout?.on('data', (chunk) => {
      output += String(chunk)
      if (command.includes('python.exe')) localFixtureOutput += String(chunk)
      if (output.includes(ready)) { clearTimeout(timer); resolve(child) }
    })
    child.stderr?.on('data', (chunk) => {
      output += String(chunk)
      if (command.includes('python.exe')) localFixtureOutput += String(chunk)
    })
    child.on('exit', (code) => { clearTimeout(timer); reject(new Error(`local fixture exited ${code}: ${output}`)) })
  })
}

async function stopProcess(child: ChildProcess | undefined): Promise<void> {
  await stopSpawnedProcess(child)
}


async function openBindingDialog(page: Page, direction: 'INPUT' | 'OUTPUT') {
  await page.getByRole('button', { name: direction === 'INPUT' ? /01.*选择 L2 输入/ : /03.*绑定可控 L2 输出/ }).click()
  await expect(page.getByRole('dialog')).toBeVisible()
}

async function applyBindingDialog(page: Page) {
  await page.getByRole('button', { name: '应用绑定', exact: true }).click()
  await expect(page.getByRole('dialog')).not.toBeVisible()
}
