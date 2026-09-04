import { expect, test, type Page, type Route } from '@playwright/test'

const now = '2026-09-05T00:00:00+00:00'
const entities = [
  { id: 'entity-soc', node_id: 'node-ess', node_type: 'ESS', node_display_name: '1#储能', definition_id: 'ess.soc', display_name: 'SOC', data_type: 'FLOAT', unit: '%', direction: 'R', freshness_seconds: 10, confirmed: true },
  { id: 'entity-limit', node_id: 'node-pcs', node_type: 'PCS', node_display_name: '1#PCS', definition_id: 'pcs.max_discharge_limit', display_name: '最大放电功率限值', data_type: 'FLOAT', unit: 'kW', direction: 'RW', freshness_seconds: 10, confirmed: true },
]

function revision(id: string, lifecycle: 'DRAFT' | 'PUBLISHED', body: any = {}) {
  return {
    id,
    strategy_id: 'strategy-1',
    revision: lifecycle === 'DRAFT' ? 1 : 2,
    lifecycle,
    trigger_kind: 'FIXED_TICK',
    site_timezone: 'Asia/Shanghai',
    jdm_content: body.jdm_content || { nodes: [], edges: [] },
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

async function installApi(page: Page) {
  let strategy: any = null
  let events: any[] = []
  const calls: string[] = []
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
    if (path === '/entity-instances' && method === 'GET') return json(route, { items: entities, total: entities.length })
    if (path.endsWith('/realtime') && method === 'GET') {
      const id = path.split('/')[2]
      return json(route, { entity_instance_id: id, definition_id: id === 'entity-soc' ? 'ess.soc' : 'pcs.max_discharge_limit', node_id: 'node-1', node_key: 'node', value: id === 'entity-soc' ? 50 : 156.8, data_type: 'FLOAT', unit: id === 'entity-soc' ? '%' : 'kW', observed_at: now, quality: 192, age_ms: 0, fresh: true, quality_good: true, processing_revision_id: 'processing-1', configuration_revision: 7 })
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
  return { calls, getStrategy: () => strategy }
}

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
