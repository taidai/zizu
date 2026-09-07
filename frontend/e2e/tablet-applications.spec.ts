import { expect, test, type Page, type Route } from '@playwright/test'
import { spawn, type ChildProcess } from 'node:child_process'
import path from 'node:path'
import { openEngineeringPage } from './support/tabletNavigation'

const now = '2026-09-07T08:00:00+08:00'
const tabletPort = 4186
const tabletBaseUrl = `http://127.0.0.1:${tabletPort}`
let vite: ChildProcess | undefined

test.setTimeout(180_000)

test.beforeAll(async () => {
  vite = spawn(process.execPath, [path.resolve('node_modules/vite/bin/vite.js'), 'preview', '--host', '127.0.0.1', '--port', String(tabletPort)], {
    cwd: process.cwd(),
    env: process.env,
    windowsHide: true,
  })
  await new Promise<void>((resolve, reject) => {
    let output = ''
    const timeout = setTimeout(() => reject(new Error(`tablet Vite did not start: ${output}`)), 60_000)
    const collect = (chunk: Buffer) => {
      output += String(chunk)
      if (output.includes('Local:')) {
        clearTimeout(timeout)
        resolve()
      }
    }
    vite?.stdout?.on('data', collect)
    vite?.stderr?.on('data', collect)
    vite?.once('exit', (code) => {
      clearTimeout(timeout)
      reject(new Error(`tablet Vite exited ${code}: ${output}`))
    })
  })
})

test.afterAll(() => {
  vite?.kill()
})

function staleStrategyView() {
  const graph = {
    nodes: [
      { id: 'input', type: 'inputNode', name: 'Input' },
      { id: 'decision', type: 'expressionNode', name: '完整规则', content: { expressions: [] } },
      { id: 'output', type: 'outputNode', name: 'Output' },
    ],
    edges: [
      { id: 'input-decision', sourceId: 'input', targetId: 'decision', type: 'edge' },
      { id: 'decision-output', sourceId: 'decision', targetId: 'output', type: 'edge' },
    ],
  }
  const draft = {
    id: 'draft-1', strategy_id: 'strategy-1', revision: 1, lifecycle: 'DRAFT', trigger_kind: 'DATA_CHANGE',
    site_timezone: 'Asia/Shanghai', jdm_content: graph, content_digest: 'a'.repeat(64),
    base_configuration_revision: 7, bindings: [], created_by: 'engineer:tablet', created_at: now,
    published_by: null, published_at: null,
  }
  return {
    id: 'strategy-1', name: '完整图策略', description: null, active_revision_id: null, enabled: false,
    runtime_health: 'READY', last_trigger_key: null, last_evaluated_at: null, last_desired: null,
    last_actual: null, last_evidence: null, failure_code: null, created_at: now, updated_at: now,
    draft, active_revision: null, published_revision: null,
  }
}

function applicationStrategy(multipleTables = false) {
  const graph = {
    nodes: [
      { id: 'input', type: 'inputNode', name: 'Input', metadata: { keep: true } },
      { id: 'rules', type: 'decisionTableNode', name: '通用规则', content: {
        hitPolicy: 'first',
        inputs: [{ id: 'room_temp', name: '室温', type: 'expression', field: 'room_temp', metadata: { source: 'fixture' } }],
        outputs: [{ id: 'fan_enable', name: '风机启停', type: 'expression', field: 'fan_enable' }],
        rules: [{ _id: 'hot', room_temp: '> 30', fan_enable: 'true' }],
        metadata: { owner: 'plant-a' },
      } },
      ...(multipleTables ? [{ id: 'rules-2', type: 'decisionTableNode', name: '第二张表', content: { hitPolicy: 'first', inputs: [], outputs: [], rules: [] } }] : []),
      { id: 'vendor', type: 'vendorNode', content: { preserve: 'always' } },
    ],
    edges: [{ id: 'edge-1', sourceId: 'input', targetId: 'rules', metadata: { keep: true } }],
    metadata: { owner: 'fixture' },
  }
  const draft = {
    id: 'draft-native', strategy_id: 'strategy-native', revision: 4, lifecycle: 'DRAFT', trigger_kind: 'DATA_CHANGE',
    site_timezone: 'Asia/Shanghai', jdm_content: graph, content_digest: 'b'.repeat(64), base_configuration_revision: 7,
    bindings: [
      { direction: 'INPUT', binding_key: 'room_temp', ordinal: 0, entity_instance_id: 'temperature-1', expected_data_type: 'FLOAT', unit: 'C', freshness_seconds: 10 },
      { direction: 'OUTPUT', binding_key: 'fan_enable', ordinal: 0, entity_instance_id: 'fan-1', expected_data_type: 'BOOL', unit: null, freshness_seconds: 10 },
    ],
    created_by: 'engineer:tablet', created_at: now, published_by: null, published_at: null,
  }
  return {
    id: 'strategy-native', name: multipleTables ? '多表策略' : '通用温控策略', description: null, active_revision_id: null, enabled: false,
    runtime_health: 'READY', last_trigger_key: null, last_evaluated_at: null, last_desired: null, last_actual: null,
    last_evidence: null, failure_code: null, created_at: now, updated_at: now, draft, active_revision: null, published_revision: null,
  }
}

async function installReadOnlyApi(page: Page, staleCode?: string, fixture?: 'alarms' | 'native' | 'multiple') {
  const writes: string[] = []
  const staleStrategy = staleCode ? staleStrategyView() : null
  let fixtureStrategy = fixture === 'native' || fixture === 'multiple' ? applicationStrategy(fixture === 'multiple') : null
  await page.routeWebSocket('**/api/v1/ws/data-frames', (socket) => {
    socket.onMessage((message) => {
      const body = JSON.parse(String(message))
      if (body.authenticate) socket.send(JSON.stringify({ type: 'authenticated' }))
      if (body.subscribe) socket.send(JSON.stringify({ type: 'subscribed' }))
    })
  })
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname.replace('/api/v1', '')
    const method = request.method()
    if (method !== 'GET' && path !== '/auth/ws-ticket') writes.push(`${method} ${path}`)
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
    if (path === '/auth/me') return json({ user: { id: 'admin-tablet', username: 'tablet-admin', role: 'admin' } })
    if (path === '/health') return json({
      status: 'healthy', version: 'local', uptime_seconds: 60,
      pipeline: { status: 'running', messages_received: 10, points_written_db: 10, last_message_at: now },
      components: { timescaledb: { status: 'connected' }, mqtt: { status: 'connected' }, neuron: { status: 'connected' } },
    })
    if (path === '/ems-workbench') return json({ workbench_id: 'default', configuration_revision: 7, navigation: [], groups: [], kpis: [], trends: [], alarms: { visible: true }, controls: { visible: false, entities: [] } })
    if (path === '/auth/ws-ticket') return json({ ticket: 'isolated-applications-ui' })
    if (path === '/runtime/frame-snapshot') return json({ type: 'frame_snapshot', node_id: null, cursor: 'isolated', frame_sequence: 0, frame_time: null, configuration_revision: 7, frame_status: null, failure: null, backlog_frames: 0, l0: [], l2: [] })
    if (path === '/categories') return json({ categories: [] })
    if (path === '/alarms/counts') return json({ counts: {} })
    if (fixture === 'alarms' && /^\/alarm-events\/alarm-2\/acknowledgements$/.test(path) && method === 'POST') return json({ detail: { code: 'ALARM_STATE_CONFLICT' } }, 409)
    if (fixture === 'alarms' && /^\/alarm-events\/alarm-\d+\/acknowledgements$/.test(path) && method === 'POST') return json({ state: 'active_acknowledged' })
    if (fixture === 'alarms' && /^\/alarm-events\/alarm-\d+\/transitions$/.test(path)) return json({ items: [{ id: 'transition-1', event_id: path.split('/')[2], from_state: 'pending', to_state: 'active_unacknowledged', occurred_at: now, code: 'ALARM_ACTIVATED', evidence: { source_ref: 'frame-88', value: 42, quality: 192 }, actor: null, note: null }], total: 1 })
    if (fixture === 'alarms' && /^\/alarm-events\/alarm-\d+$/.test(path)) {
      const id = path.split('/')[2]
      return json({ id, definition_id: 'temperature.high', entity_instance_id: 'temperature-1', state: 'active_unacknowledged', severity: 'MAJOR', pending_at: now, active_at: now, acknowledged_at: null, acknowledged_by: null, acknowledgement_note: null, recovered_at: null, node_name: '储能柜 1', entity_name: '柜内温度', alarm_name: `高温告警 ${id.split('-')[1]}`, duration_seconds: 120, archived_at: null, archived_by: null })
    }
    if (fixture === 'alarms' && path === '/alarm-events') {
      const url = new URL(request.url())
      const pageSize = Number(url.searchParams.get('page_size') || 10)
      const pageNumber = Number(url.searchParams.get('page') || 1)
      const items = Array.from({ length: 11 }, (_, index) => ({ id: `alarm-${index + 1}`, definition_id: 'temperature.high', entity_instance_id: 'temperature-1', state: 'active_unacknowledged', severity: index === 0 ? 'CRITICAL' : 'MAJOR', pending_at: now, active_at: now, acknowledged_at: null, acknowledged_by: null, recovered_at: null, node_name: '储能柜 1', entity_name: '柜内温度', alarm_name: `高温告警 ${index + 1}`, duration_seconds: 120, archived_at: null, archived_by: null }))
      return json({ items: items.slice((pageNumber - 1) * pageSize, pageNumber * pageSize), total: 11, page: pageNumber, page_size: pageSize, total_pages: Math.ceil(11 / pageSize), summary: { active: 11, unacknowledged: 11, critical: 1 } })
    }
    if (path.startsWith('/alarm-events')) return json({ items: [], total: 0, page: 1, page_size: 50, total_pages: 1, summary: { active: 0, unacknowledged: 0, critical: 0 } })
    if (path === '/alarms/entities') return json({ items: [] })
    if (path === '/dispatch-strategies') return json({ strategies: fixtureStrategy ? [fixtureStrategy] : staleStrategy ? [staleStrategy] : [] })
    if (path === '/dispatch-strategies/strategy-native/draft' && method === 'PUT' && fixtureStrategy) {
      const body = request.postDataJSON()
      const nodes = body.jdm_content?.nodes || []
      const preserved = body.jdm_content?.metadata?.owner === 'fixture'
        && nodes.some((node: { id: string; content?: { preserve?: string } }) => node.id === 'vendor' && node.content?.preserve === 'always')
        && nodes.some((node: { id: string; content?: { metadata?: { owner?: string }; inputs?: { metadata?: { source?: string } }[] } }) => node.id === 'rules' && node.content?.metadata?.owner === 'plant-a' && node.content?.inputs?.[0]?.metadata?.source === 'fixture')
        && body.jdm_content?.edges?.[0]?.metadata?.keep === true
        && body.bindings?.length === 2
      if (!preserved) return json({ detail: { code: 'ROUND_TRIP_LOSS', message: 'fixture graph or bindings lost' } }, 409)
      fixtureStrategy = {
        ...fixtureStrategy,
        name: body.name,
        draft: { ...fixtureStrategy.draft!, ...body, content_digest: 'c'.repeat(64) },
      }
      return json(fixtureStrategy)
    }
    if (path === '/dispatch-strategies/strategy-native') return json(fixtureStrategy)
    if (path === '/dispatch-strategies/strategy-native/events') return json({ items: [], next_cursor: null })
    if (path === '/dispatch-strategies/strategy-native/simulate' && method === 'POST') return json({ status: 'EVALUATED', reason_code: null, frame_sequence: 18, configuration_revision: 7, snapshot: {}, engine_inputs: {}, matched_rules: ['hot'], decision: { fan_enable: true }, proposed_intents: [] })
    if (path === '/dispatch-strategies/strategy-1') return json(staleStrategy)
    if (path === '/dispatch-strategies/strategy-1/events') return json({ items: [], next_cursor: null })
    if (path === '/dispatch-strategies/strategy-1/simulate' && method === 'POST') return json({
      status: 'EVALUATED', reason_code: null, frame_sequence: 18, configuration_revision: 7,
      snapshot: {}, engine_inputs: {}, matched_rules: [], decision: {}, proposed_intents: [],
    })
    if (path === '/dispatch-strategies/strategy-1/draft' && method === 'PUT' && staleCode) {
      return json({ detail: { code: staleCode, message: 'stale' } }, 409)
    }
    if (path === '/entity-instances') return json({ items: fixtureStrategy ? [
      { id: 'temperature-1', node_id: 'node-1', node_type: 'STORAGE', node_display_name: '储能柜 1', definition_id: 'room.temperature', display_name: '柜内温度', data_type: 'FLOAT', unit: 'C', direction: 'R', freshness_seconds: 10, confirmed: true, control_eligible: false },
      { id: 'mode-1', node_id: 'node-1', node_type: 'STORAGE', node_display_name: '储能柜 1', definition_id: 'site.mode', display_name: '运行模式', data_type: 'STRING', unit: null, direction: 'R', freshness_seconds: 10, confirmed: true, control_eligible: false },
      { id: 'fan-1', node_id: 'node-1', node_type: 'STORAGE', node_display_name: '储能柜 1', definition_id: 'fan.enable', display_name: '风机启停', data_type: 'BOOL', unit: null, direction: 'RW', freshness_seconds: 10, confirmed: true, control_eligible: true },
      { id: 'unsafe-output', node_id: 'node-1', node_type: 'STORAGE', node_display_name: '储能柜 1', definition_id: 'unsafe.target', display_name: '无控制合同目标', data_type: 'FLOAT', unit: 'kW', direction: 'W', freshness_seconds: 10, confirmed: true, control_eligible: false },
    ] : [], total: fixtureStrategy ? 4 : 0 })
    if (path === '/pipeline/config') return json({ batch_size: 50, flush_interval_sec: 1 })
    if (path === '/mqtt-config') return json({ mqtt_telemetry_topic: '/neuron/#', persisted: null, effective_topics: [] })
    if (path === '/admin/alarm-http-notifications') return json([])
    if (path === '/nodes') return json({ nodes: [] })
    if (path.startsWith('/telemetry')) return json({ points: [], has_more: false, next_cursor: null })
    if (path.startsWith('/fault-maps')) return json({ items: [], total: 0 })
    if (path.startsWith('/nanomq/clients')) return json({ clients: [] })
    if (path.startsWith('/nanomq/subscriptions')) return json({ subscriptions: [] })
    if (path.startsWith('/nanomq/acl')) return json({ rules: [] })
    if (path.startsWith('/nanomq/config')) return json({})
    if (path.startsWith('/nanomq/status')) return json({ running: false })
    return json({})
  })
  return writes
}

for (const [code, message] of [
  ['STRATEGY_DRAFT_STALE', '策略草稿已被其他人修改，请重新加载后再试算。'],
  ['DATA_FRAME_CONFIGURATION_STALE', '实体配置已变化，请重新加载策略后再试算。'],
] as const) {
  test(`${code} 拒绝使旧试算失效并要求重新加载`, async ({ page }) => {
    await installReadOnlyApi(page, code)
    await page.goto(tabletBaseUrl, { waitUntil: 'domcontentloaded' })
    await openNavigation(page, '调度策略')
    await expect(page.getByLabel('策略名称')).toHaveValue('完整图策略')
    await page.getByRole('button', { name: '试算', exact: true }).click()
    await expect(page.getByTestId('strategy-simulation')).toBeVisible()

    await page.getByRole('button', { name: '保存草稿', exact: true }).click()

    await expect(page.getByTestId('dispatch-strategy-page').getByRole('alert')).toContainText(message)
    await expect(page.getByTestId('strategy-simulation')).not.toBeVisible()
    await expect(page.getByRole('button', { name: '保存草稿', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: '试算', exact: true })).toBeDisabled()
    await expect(page.getByRole('button', { name: '重新加载策略', exact: true })).toBeVisible()
    await page.getByRole('button', { name: '重新加载策略', exact: true }).click()
    await expect(page.getByRole('button', { name: '重新加载策略', exact: true })).not.toBeVisible()
  })
}

test('告警事件按10/20条展示、当前页确认逐项报错并读取真实详情流转', async ({ page }) => {
  await installReadOnlyApi(page, undefined, 'alarms')
  await page.goto(tabletBaseUrl, { waitUntil: 'domcontentloaded' })
  await openNavigation(page, '告警中心')

  await expect(page.getByLabel('每页条数')).toHaveValue('10')
  await expect(page.getByTestId('alarm-event-table').locator('tbody tr')).toHaveCount(10)
  await page.getByLabel('选择当前页可确认告警').check()
  await page.getByRole('button', { name: '确认所选（10）' }).click()
  await expect(page.getByTestId('tablet-alarm-applications').getByRole('status')).toContainText('alarm-2')
  await expect(page.getByRole('button', { name: '确认所选（0）' })).toBeDisabled()
  await page.getByLabel('选择告警 alarm-1', { exact: true }).check()
  await page.getByRole('button', { name: '›' }).click()
  await expect(page.getByRole('button', { name: '确认所选（0）' })).toBeDisabled()
  await page.getByRole('button', { name: '‹' }).click()

  await page.getByText('高温告警 1', { exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '告警详情' })
  await expect(dialog).toContainText('temperature.high')
  await expect(dialog).toContainText('ALARM_ACTIVATED')
  await expect(dialog).toContainText('frame-88')
  await dialog.getByRole('button', { name: '关闭' }).click()

  await page.getByLabel('每页条数').selectOption('20')
  await expect(page.getByTestId('alarm-event-table').locator('tbody tr')).toHaveCount(11)
  await page.getByLabel('选择告警 alarm-1', { exact: true }).check()
  await page.getByRole('button', { name: '已确认', exact: true }).click()
  await expect(page.getByRole('button', { name: '确认所选（0）' })).toBeDisabled()
})

test('唯一通用表使用原生编辑器和泛型L2绑定，任一绑定变化使试算失效', async ({ page }) => {
  await installReadOnlyApi(page, undefined, 'native')
  await page.goto(tabletBaseUrl, { waitUntil: 'domcontentloaded' })
  await openNavigation(page, '调度策略')

  await expect(page.getByTestId('native-decision-table')).toBeVisible()
  await expect(page.getByText('不限制为 SOC 或固定时段。')).toBeVisible()
  await expect(page.getByLabel('输入 1 别名')).toHaveValue('room_temp')
  await expect(page.getByLabel('输出 1 别名')).toHaveValue('fan_enable')
  await expect(page.getByLabel('输入 1 实体')).toContainText('柜内温度')
  await expect(page.getByLabel('输入 1 实体')).toContainText('运行模式')
  await expect(page.getByLabel('输出 1 实体')).toContainText('风机启停')
  await expect(page.getByLabel('输出 1 实体')).not.toContainText('无控制合同目标')

  await page.getByRole('button', { name: '试算', exact: true }).click()
  await expect(page.getByTestId('strategy-simulation')).toBeVisible()
  await page.getByLabel('输入 1 别名').fill('ambient_temperature')
  await expect(page.getByTestId('strategy-simulation')).not.toBeVisible()
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByTestId('dispatch-strategy-page').getByRole('status')).toContainText('草稿已保存')
  await page.reload({ waitUntil: 'domcontentloaded' })
  await openNavigation(page, '调度策略')
  await expect(page.getByLabel('输入 1 别名')).toHaveValue('ambient_temperature')
})

test('多决策表不进入简化表，完整图仍是唯一正式编辑入口', async ({ page }) => {
  await installReadOnlyApi(page, undefined, 'multiple')
  await page.goto(tabletBaseUrl, { waitUntil: 'domcontentloaded' })
  await openNavigation(page, '调度策略')
  await expect(page.getByTestId('native-decision-table')).not.toBeVisible()
  await expect(page.getByText(/不是可无损往返的唯一决策表/)).toBeVisible()
  await expect(page.getByRole('button', { name: '打开完整规则图' })).toBeVisible()
})

async function expectTouchTargets(page: Page, names: string[]) {
  for (const name of names) {
    const button = page.getByRole('button', { name, exact: true })
    await expect(button).toBeVisible()
    expect((await button.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  }
}

async function openNavigation(page: Page, name: '告警中心' | '调度策略' | '系统工具') {
  await openEngineeringPage(page, name)
}

for (const viewport of [{ width: 1280, height: 800 }, { width: 1024, height: 768 }]) {
  test(`告警、调度和系统工具在 ${viewport.width}x${viewport.height} 保持触控可达且只读打开`, async ({ page }) => {
    const runtimeErrors: string[] = []
    page.on('pageerror', (error) => runtimeErrors.push(error.message))
    page.on('console', (message) => { if (message.type() === 'error') runtimeErrors.push(message.text()) })
    page.on('requestfailed', (request) => {
      if (request.failure()?.errorText !== 'net::ERR_ABORTED') runtimeErrors.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`)
    })
    await page.setViewportSize(viewport)
    const writes = await installReadOnlyApi(page)
    await page.goto(tabletBaseUrl, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1_000)
    expect(runtimeErrors).toEqual([])

    await openNavigation(page, '告警中心')
    await expect(page.getByTestId('tablet-alarm-applications')).toBeVisible()
    await expectTouchTargets(page, ['当前告警', '通知记录', '告警规则'])
    await expect(page.getByRole('button', { name: '当前告警', exact: true })).toHaveCSS('background-color', 'rgb(238, 228, 206)')

    await openNavigation(page, '调度策略')
    await expect(page.locator('[data-tablet-applications="dispatch"]')).toBeVisible()
    await expectTouchTargets(page, ['新建 2充2放'])

    await openNavigation(page, '系统工具')
    await page.waitForTimeout(1_000)
    expect(runtimeErrors).toEqual([])
    await expect(page.getByTestId('tablet-admin-applications')).toBeVisible()
    await expectTouchTargets(page, ['保存配置', '保存并重订阅', '执行', '清空表'])

    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
    expect(writes).toEqual([])
  })
}
