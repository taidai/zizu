import { expect, test, type Page, type Route } from '@playwright/test'
import { buildTwoChargeTwoDischargeJdm } from '../src/components/dispatch-strategy/dispatchStrategyModel.mjs'

const now = '2026-09-05T00:00:00+00:00'
const entities = [
  { id: 'entity-soc', node_id: 'node-ess', node_type: 'ESS', node_display_name: '1#储能', definition_id: 'bms.soc', display_name: 'SOC', data_type: 'FLOAT', unit: '%', direction: 'R', freshness_seconds: 10, confirmed: true },
  { id: 'entity-limit', node_id: 'node-pcs', node_type: 'PCS', node_display_name: '1#PCS', definition_id: 'pcs.max_discharge_limit', display_name: '最大放电功率限值', data_type: 'FLOAT', unit: 'kW', direction: 'RW', freshness_seconds: 10, confirmed: true },
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

async function installApi(page: Page, initialStrategy: any = null, entityRows = entities) {
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
    if (path === '/dispatch-strategies/strategy-1/events' && method === 'GET') return json(route, { items: events, next_cursor: null })
    if (path === '/dispatch-strategies/strategy-1/draft' && method === 'PUT') {
      const body = request.postDataJSON()
      savedDrafts.push(body)
      strategy = { ...strategy, name: body.name, runtime_health: 'READY', draft: revision('draft-1', 'DRAFT', body) }
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
      events = [{ id: 'event-1', occurred_at: now, event_kind: 'DECISION_CHANGED', trigger_kind: 'FIXED_TICK', trigger_key: 'tick:1', frame_sequence: 42, configuration_revision: 7, snapshot_evidence: {}, decision: { matched_rule: 'discharge-1' }, intent_summary: [{ value: 80 }], control_command_id: 'command-1', control_status: 'confirmed', reason_code: null }]
      return json(route, strategy)
    }
    if (path === '/dispatch-strategies/strategy-1/disable' && method === 'POST') {
      strategy = { ...strategy, enabled: false }
      return json(route, strategy)
    }
    return json(route, { detail: { code: 'UNMOCKED', message: `${method} ${path}` } }, 500)
  })
  return { calls, savedDrafts, getStrategy: () => strategy }
}

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
  const api = await installApi(page, original)
  await page.goto('/')
  await page.getByRole('button', { name: '调度策略' }).click()
  await expect(page.getByLabel('策略名称')).toHaveValue(original.name)
  await expect.soft(page.getByLabel('时段 1 功率目标')).not.toBeVisible()
  await expect.soft(page.getByLabel('SOC 输入实体')).toBeDisabled()
  await expect.soft(page.getByLabel('功率控制实体')).toBeDisabled()
  await expect.soft(page.getByText(/无法由 2充2放表无损表示/)).toBeVisible()
  await page.getByRole('button', { name: '打开完整规则图', exact: true }).click()
  await expect(page.getByText('完整决策', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: '收起完整规则图', exact: true }).click()
  await page.getByLabel('策略名称').fill('只修改显示名称')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('草稿已保存')
  expect(api.savedDrafts).toHaveLength(1)
  const submitted = api.savedDrafts[0]
  expect(submitted.name).toBe('只修改显示名称')
  expect.soft(submitted.jdm_content).toEqual(expectedRevision.jdm_content)
  expect.soft(submitted.trigger_kind).toBe('DATA_CHANGE')
  expect.soft(submitted.site_timezone).toBe('Europe/Berlin')
  expect.soft(submitted.bindings).toEqual(expectedRevision.bindings)
  expect(pageErrors).toEqual([])
})

test('内置表编辑直接更新完整图并保留原绑定契约和触发', async ({ page }) => {
  const original = strategyView()
  original.draft.trigger_kind = 'DATA_CHANGE'
  original.draft.bindings = [
    { direction: 'INPUT', binding_key: 'soc', ordinal: 0, entity_instance_id: 'entity-soc', expected_data_type: 'FLOAT', unit: '%', freshness_seconds: 30 },
    { direction: 'OUTPUT', binding_key: 'power-target', ordinal: 0, entity_instance_id: 'entity-limit', expected_data_type: 'FLOAT', unit: 'kW', freshness_seconds: 30 },
  ]
  const api = await installApi(page, original)
  await page.goto('/')
  await page.getByRole('button', { name: '调度策略' }).click()
  await page.getByLabel('时段 1 功率目标').fill('-25')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('草稿已保存')
  expect(api.savedDrafts[0].jdm_content.nodes[1].content.rules[0].target).toBe('-25')
  expect(api.savedDrafts[0].bindings).toEqual(original.draft.bindings)
  expect(api.savedDrafts[0].trigger_kind).toBe('DATA_CHANGE')
  await page.reload()
  await page.getByRole('button', { name: '调度策略' }).click()
  await expect(page.getByLabel('时段 1 功率目标')).toHaveValue('-25')
})

test('候选只允许标准 SOC 百分比输入和 kW 数值控制输出', async ({ page }) => {
  await installApi(page, strategyView(), [
    ...entities,
    { ...entities[0], id: 'storage-soc', definition_id: 'storage.soc', data_type: 'INT' },
    { ...entities[0], id: 'misnamed-current', definition_id: 'bms.current', display_name: 'SOC', unit: 'A' },
    { ...entities[0], id: 'legacy-soc', definition_id: 'ess.soc' },
    { ...entities[0], id: 'soc-ratio', unit: 'ratio' },
    { ...entities[0], id: 'soc-bool', data_type: 'BOOL' },
    { ...entities[0], id: 'soc-unconfirmed', confirmed: false },
    { ...entities[1], id: 'voltage-output', unit: 'V' },
    { ...entities[1], id: 'boolean-output', data_type: 'BOOL' },
  ])
  await page.goto('/')
  await page.getByRole('button', { name: '调度策略' }).click()
  await expect(page.getByLabel('策略名称')).toBeVisible()
  expect(await page.getByLabel('SOC 输入实体').locator('option').evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value))).toEqual(['', 'entity-soc', 'storage-soc'])
  expect(await page.getByLabel('功率控制实体').locator('option').evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value))).toEqual(['', 'entity-limit'])
})

test('已有错误 SOC 绑定明确阻止保存且不自动选择其他实体', async ({ page }) => {
  const original = strategyView()
  original.draft.bindings = [
    { direction: 'INPUT', binding_key: 'soc', ordinal: 0, entity_instance_id: 'wrong-soc', expected_data_type: 'FLOAT', unit: 'A', freshness_seconds: 10 },
    { direction: 'OUTPUT', binding_key: 'power-target', ordinal: 0, entity_instance_id: 'entity-limit', expected_data_type: 'FLOAT', unit: 'kW', freshness_seconds: 10 },
  ]
  const api = await installApi(page, original, [...entities, { ...entities[0], id: 'wrong-soc', definition_id: 'bms.current', display_name: 'SOC', unit: 'A' }])
  await page.goto('/')
  await page.getByRole('button', { name: '调度策略' }).click()
  await expect(page.getByLabel('策略名称')).toBeVisible()
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByTestId('dispatch-strategy-page').getByRole('alert')).toContainText(/SOC.*绑定.*不符合/, { timeout: 3000 })
  expect(api.savedDrafts).toHaveLength(0)
  await expect(page.getByLabel('SOC 输入实体')).toHaveValue('wrong-soc')
  await page.getByLabel('SOC 输入实体').selectOption('entity-soc')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('草稿已保存')
  expect(api.savedDrafts[0].bindings[0].entity_instance_id).toBe('entity-soc')
})

test('没有合法 SOC 候选时提示先建立标准百分比实体', async ({ page }) => {
  await installApi(page, strategyView(), [entities[1]])
  await page.goto('/')
  await page.getByRole('button', { name: '调度策略' }).click()
  await expect(page.getByLabel('策略名称')).toBeVisible()
  await expect(page.getByText(/先.*建立.*标准 SOC.*百分比/)).toBeVisible({ timeout: 3000 })
})

test('午夜结束时间保持 24:00 原义且无效编辑不能保存旧图', async ({ page }) => {
  const original = strategyView()
  original.draft.jdm_content = buildTwoChargeTwoDischargeJdm([
    { key: 'night', start: '22:00', end: '24:00', action: 'CHARGE', target: -10, socMin: 10, socMax: 90 },
  ], 0)
  original.draft.bindings = [
    { direction: 'INPUT', binding_key: 'soc', ordinal: 0, entity_instance_id: 'entity-soc', expected_data_type: 'FLOAT', unit: '%', freshness_seconds: 10 },
    { direction: 'OUTPUT', binding_key: 'power-target', ordinal: 0, entity_instance_id: 'entity-limit', expected_data_type: 'FLOAT', unit: 'kW', freshness_seconds: 10 },
  ]
  const api = await installApi(page, original)
  await page.goto('/')
  await page.getByRole('button', { name: '调度策略' }).click()
  await expect(page.getByLabel('策略名称')).toBeVisible()
  await expect(page.getByLabel('时段 1 结束')).toHaveValue('24:00', { timeout: 3000 })
  await page.getByLabel('其他时段安全目标').fill('')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByTestId('dispatch-strategy-page').getByRole('alert')).toContainText('其他时段安全目标')
  expect(api.savedDrafts).toHaveLength(0)
  await page.getByLabel('其他时段安全目标').fill('1')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('草稿已保存')
  expect(api.savedDrafts[0].jdm_content.nodes[1].content.rules[0].site_local_minute).toBe('site_local_minute >= 1320 && site_local_minute < 1440')
  expect(api.savedDrafts[0].jdm_content.nodes[1].content.rules.at(-1).target).toBe('1')
})

test('2充2放从 L2 绑定到控制回读只走一条策略流程', async ({ page }) => {
  const consoleErrors: string[] = []
  page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text()) })
  const api = await installApi(page)
  await page.goto('/')
  await page.getByRole('button', { name: '调度策略' }).click()
  await page.getByRole('button', { name: '新建 2充2放' }).click()
  await expect(page.getByLabel('策略名称')).toHaveValue('2充2放调度策略')

  await page.getByLabel('SOC 输入实体').selectOption('entity-soc')
  await page.getByLabel('功率控制实体').selectOption('entity-limit')
  await expect(page.getByText(/质量：正常/).first()).toBeVisible()

  await page.getByRole('button', { name: '试算', exact: true }).click()
  await expect(page.getByTestId('strategy-simulation')).toContainText('帧 42')
  await expect(page.getByTestId('strategy-simulation')).toContainText('discharge-1')
  await expect(page.getByTestId('strategy-simulation')).toContainText('power-target=80')

  await page.getByRole('button', { name: '发布', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('已发布为不可变版本')
  await page.getByRole('button', { name: '启用', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('下一个整分钟')
  await page.getByRole('region', { name: '策略状态' }).getByText('已启用', { exact: true }).waitFor()

  await page.getByRole('region', { name: '策略状态' }).scrollIntoViewIfNeeded()
  await page.getByRole('region', { name: '4. 关键事件与控制回读' }).getByRole('button', { name: '刷新' }).click()
  await expect(page.getByRole('cell', { name: 'command-1' })).toBeVisible()
  await expect(page.getByRole('cell', { name: 'confirmed' })).toBeVisible()
  await page.getByRole('button', { name: '停用', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('不再产生新的控制意图')

  expect(api.getStrategy().enabled).toBe(false)
  expect(api.calls).toContain('PUT /dispatch-strategies/strategy-1/draft')
  expect(api.calls).toContain('POST /dispatch-strategies/strategy-1/simulate')
  expect(api.calls).toContain('POST /dispatch-strategies/strategy-1/publish')
  expect(api.calls).toContain('POST /dispatch-strategies/strategy-1/enable')
  expect(api.calls).toContain('POST /dispatch-strategies/strategy-1/disable')
  expect(consoleErrors, api.calls.join('\n')).toEqual([])
})
