import { expect, test, type Page, type Route } from '@playwright/test'
import { execFile, spawn, type ChildProcess } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { promisify } from 'node:util'
import { buildTwoChargeTwoDischargeJdm } from '../src/components/dispatch-strategy/dispatchStrategyModel.mjs'

const execFileAsync = promisify(execFile)
let localFixtureOutput = ''

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

async function installApi(page: Page, initialStrategy: any = null, entityRows = entities, currentConfigurationRevision = 7) {
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
  await page.getByRole('button', { name: '调度策略' }).click()
  await expect(page.getByLabel('SOC 输入实体')).toBeDisabled()
  await expect(page.getByLabel('功率控制实体')).toBeDisabled()

  await page.getByRole('button', { name: '保存草稿', exact: true }).click()

  await expect(page.getByRole('status')).toContainText('草稿已保存')
  expect(api.savedDrafts).toHaveLength(1)
  expect(api.savedDrafts[0].base_configuration_revision).toBe(6)
  expect(api.savedDrafts[0].jdm_content).toEqual(originalGraph)
  expect(api.savedDrafts[0].bindings).toEqual(originalBindings)
  expect(api.getStrategy().draft.base_configuration_revision).toBe(7)
  expect(api.getStrategy().draft.bindings.map((item: any) => item.unit)).toEqual(['%', '%', 'kW'])
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

function publishedStrategy() {
  const original = strategyView()
  original.draft = null
  original.published_revision = revision('published-1', 'PUBLISHED', { bindings: [
    { direction: 'INPUT', binding_key: 'soc', ordinal: 0, entity_instance_id: 'entity-soc', expected_data_type: 'FLOAT', unit: '%', freshness_seconds: 10 },
    { direction: 'OUTPUT', binding_key: 'power-target', ordinal: 0, entity_instance_id: 'entity-limit', expected_data_type: 'FLOAT', unit: 'kW', freshness_seconds: 10 },
  ] })
  return original
}

test('已发布策略直接试算不保存草稿、不改变发布状态', async ({ page }) => {
  const api = await installApi(page, publishedStrategy())
  await page.goto('/')
  await page.getByRole('button', { name: '调度策略' }).click()
  await expect(page.getByLabel('策略名称')).toBeVisible()
  const request = page.waitForRequest((item) => item.url().endsWith('/simulate'))
  await page.getByRole('button', { name: '试算', exact: true }).click()
  expect((await request).postDataJSON()).toEqual({ revision_id: 'published-1', expected_digest: 'c'.repeat(64) })
  await expect(page.getByTestId('strategy-simulation')).toContainText('帧 42')
  expect(api.savedDrafts).toHaveLength(0)
  await expect(page.getByRole('region', { name: '策略状态' })).not.toContainText('有未发布修改')
  expect(api.calls.filter((call) => call.startsWith('POST '))).toEqual(['POST /dispatch-strategies/strategy-1/simulate'])
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
  await page.getByRole('button', { name: '调度策略' }).click()
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
  await page.getByRole('button', { name: '调度策略' }).click()
  await expect(page.getByLabel('策略名称')).toBeVisible()
  await page.getByLabel('时段 1 功率目标').fill('-20')
  const request = page.waitForRequest((item) => item.url().endsWith('/simulate'))
  await page.getByRole('button', { name: '试算', exact: true }).click()
  expect((await request).postDataJSON()).toEqual({ revision_id: 'draft-1', expected_digest: 'b'.repeat(64) })
  await expect(page.getByTestId('strategy-simulation')).toBeVisible()
  expect(api.savedDrafts).toHaveLength(1)
  expect(api.savedDrafts[0].jdm_content.nodes[1].content.rules[0].target).toBe('-20')
  await page.getByRole('button', { name: '试算', exact: true }).click()
  await expect(page.getByTestId('strategy-simulation')).toBeVisible()
  expect(api.savedDrafts).toHaveLength(1)
  const requestsBefore = api.calls.filter((call) => call.endsWith('/simulate')).length
  await page.getByLabel('其他时段安全目标').fill('')
  await page.getByRole('button', { name: '试算', exact: true }).click()
  await expect(page.getByTestId('dispatch-strategy-page').getByRole('alert')).toContainText('其他时段安全目标')
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
    database = await createDisposablePostgresDatabase(databaseName)
    backend = await startLocalDispatchFixture(database, password, backendPort)
    frontend = await startViteForLocalFixture(backendPort, frontendPort)
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
    await page.getByRole('button', { name: '登录', exact: true }).click()
    consoleErrors.length = 0
    await page.getByRole('button', { name: '调度策略' }).click()
    const strategyId = await createStrategyDraft(page)
    await page.getByLabel('SOC 输入实体').selectOption({ index: 1 })
    await page.getByLabel('功率控制实体').selectOption({ index: 1 })
    await page.getByLabel('时段 1 功率目标').fill('0.5')
    await page.getByLabel('时段 2 功率目标').fill('0.5')
    await page.getByLabel('时段 3 功率目标').fill('0.5')
    await page.getByLabel('时段 4 功率目标').fill('0.5')
    await page.getByLabel('其他时段安全目标').fill('0.5')
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
    await page.getByRole('button', { name: '调度策略' }).click()
    const strategyId = await createStrategyDraft(page)
    await page.getByLabel('SOC 输入实体').selectOption({ index: 1 })
    await page.getByLabel('功率控制实体').selectOption({ index: 1 })
    await page.getByLabel('时段 1 功率目标').fill('0.5')
    await page.getByLabel('时段 2 功率目标').fill('0.5')
    await page.getByLabel('时段 3 功率目标').fill('0.5')
    await page.getByLabel('时段 4 功率目标').fill('0.5')
    await page.getByLabel('其他时段安全目标').fill('0.5')
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

    await page.getByRole('button', { name: '节点管理', exact: true }).click()
    await page.getByPlaceholder('搜索节点...').fill('strategy-test')
    await page.getByTitle('strategy-test').click()
    await expect(page.getByRole('region', { name: '原始数据' })).toBeVisible()
    await page.getByPlaceholder('搜索点位名称').fill('soc')
    await expect(page.getByRole('row').filter({ hasText: 'soc' })).toContainText('50.5')
    await expect(page.getByRole('row').filter({ hasText: 'soc' })).toContainText('正常')

    await page.getByRole('checkbox', { name: '选择 soc' }).check()
    const l1 = page.getByLabel('加工为实体')
    await expect(l1).toContainText('已选择 1 个原始点位')
    await l1.getByRole('button', { name: '加工为实体', exact: true }).click()
    await expect(l1.getByLabel('加工方法')).toHaveValue('passthrough')
    await expect(l1).toContainText('业务标识')

    await page.getByRole('button', { name: '标准实体', exact: true }).click()
    await expect(page.getByRole('heading', { name: '标准实体' })).toBeVisible()
    await expect(page.getByText('已生效', { exact: true })).toBeVisible()
    const socEntity = page.getByRole('button', { name: /PCS 品牌 A · bms\.soc/ })
    await expect(socEntity).toContainText('50.5')
    await socEntity.click()
    await expect(page.getByRole('region', { name: '实体来源' })).toContainText('soc')
    await page.getByRole('region', { name: '实体来源' }).getByText('技术详情', { exact: true }).click()
    await expect(page.getByRole('region', { name: '实体来源' })).toContainText('definition_id: bms.soc')
    await expect(page.getByRole('region', { name: '实体来源' })).toContainText('processing_revision_id:')

    await page.getByRole('button', { name: '调度策略', exact: true }).click()
    const strategyId = await createStrategyDraft(page)
    await page.getByLabel('SOC 输入实体').selectOption({ index: 1 })
    await page.getByLabel('功率控制实体').selectOption({ index: 1 })
    await page.getByLabel('时段 1 功率目标').fill('0.5')
    await page.getByLabel('时段 2 功率目标').fill('0.5')
    await page.getByLabel('时段 3 功率目标').fill('0.5')
    await page.getByLabel('时段 4 功率目标').fill('0.5')
    await page.getByLabel('其他时段安全目标').fill('0.5')
    await page.getByRole('button', { name: '保存草稿', exact: true }).click()
    await expect(page.getByRole('status')).toContainText('草稿已保存')

    await page.getByRole('button', { name: '告警中心', exact: true }).click()
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
  const { stdout: encodedPgpass } = await execFileAsync('wsl.exe', ['-d', 'Ubuntu', '-u', 'root', '--', 'python3', '-c', "import base64; print(base64.b64encode(open('/mnt/wsl/zizu-dispatch-recovery-20260905-c73f/rootfs/run/zizu-recovery/pgpass', 'rb').read()).decode())"])
  const pgpass = Buffer.from(encodedPgpass.trim(), 'base64').toString('utf8')
  const database = { DB_HOST: '127.0.0.1', DB_PORT: '15433', DB_USER: 'postgres', DB_PASSWORD: pgpass.trim(), DB_NAME: name }
  await execFileAsync('C:\\veighna_studio\\python.exe', ['-c', 'import os, psycopg2; c=psycopg2.connect(host=os.environ["DB_HOST"],port=os.environ["DB_PORT"],user=os.environ["DB_USER"],password=os.environ["DB_PASSWORD"],dbname="postgres"); c.autocommit=True; cur=c.cursor(); cur.execute("CREATE DATABASE " + os.environ["DB_NAME"]); c.close(); d=psycopg2.connect(host=os.environ["DB_HOST"],port=os.environ["DB_PORT"],user=os.environ["DB_USER"],password=os.environ["DB_PASSWORD"],dbname=os.environ["DB_NAME"]); d.autocommit=True; d.cursor().execute("CREATE EXTENSION timescaledb CASCADE")'], { env: { ...process.env, ...database } })
  return database
}

async function dropDisposablePostgresDatabase(database: Record<string, string>): Promise<void> {
  if (!database?.DB_NAME?.endsWith('_test')) return
  await execFileAsync('C:\\veighna_studio\\python.exe', ['-c', 'import os, psycopg2; c=psycopg2.connect(host=os.environ["DB_HOST"],port=os.environ["DB_PORT"],user=os.environ["DB_USER"],password=os.environ["DB_PASSWORD"],dbname="postgres"); c.autocommit=True; cur=c.cursor(); cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s AND pid<>pg_backend_pid()", (os.environ["DB_NAME"],)); cur.execute("DROP DATABASE " + os.environ["DB_NAME"])'], { env: { ...process.env, ...database } })
}

async function startLocalDispatchFixture(database: Record<string, string>, localPassword: string, port: number): Promise<ChildProcess> {
  const root = path.resolve(process.cwd(), '..')
  return startUntilReady('C:\\veighna_studio\\python.exe', [path.join(root, 'backend', 'scripts', 'node_management_e2e_fixture.py'), 'local-dispatch-server', '--port', String(port)], {
    ...process.env, ...database, ZIZU_LOCAL_E2E_PASSWORD: localPassword,
    NEURON_PASSWORD: localPassword, NANOMQ_API_PASSWORD: localPassword, JWT_SECRET: localPassword,
    DEPLOYMENT_MODE: 'development', AUTH_REQUIRE_HTTPS: 'false', ALLOW_INSECURE_DEV_SECRETS: 'false',
    PYTHONPATH: `C:\\Users\\chent\\AppData\\Local\\Temp\\zizu-v087-local-514b7ea4b65748a1b7673a8577fe3ef8\\python-packages;${path.join(root, 'backend')};${root}`,
  }, 'LOCAL_DISPATCH_FIXTURE')
}

async function startViteForLocalFixture(backendPort: number, port: number): Promise<ChildProcess> {
  return startUntilReady('npm.cmd', ['run', 'dev', '--', '--host', '127.0.0.1', '--port', String(port)], {
    ...process.env, ZIZU_DEV_PROXY_TARGET: `http://127.0.0.1:${backendPort}`,
  }, 'Local:')
}

async function createStrategyDraft(page: Page): Promise<string> {
  const created = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'POST'
      && url.pathname === '/api/v1/dispatch-strategies'
  })
  await page.getByRole('button', { name: '新建 2充2放' }).click()
  const strategyId = (await (await created).json()).id as string
  const loaded = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return response.request().method() === 'GET'
      && url.pathname === `/api/v1/dispatch-strategies/${strategyId}`
  })
  await loaded
  await expect(page.getByLabel('SOC 输入实体')).toHaveValue('')
  await expect(page.getByLabel('功率控制实体')).toHaveValue('')
  return strategyId
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
  if (!child || child.exitCode !== null) return
  child.kill()
  await new Promise<void>((resolve) => child.once('exit', () => resolve()))
}
