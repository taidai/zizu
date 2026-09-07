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

async function installReadOnlyApi(page: Page, staleCode?: string) {
  const writes: string[] = []
  const staleStrategy = staleCode ? staleStrategyView() : null
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
    if (path.startsWith('/alarm-events')) return json({ items: [], total: 0, page: 1, page_size: 50, total_pages: 1, summary: { active: 0, unacknowledged: 0, critical: 0 } })
    if (path === '/alarms/entities') return json({ items: [] })
    if (path === '/dispatch-strategies') return json({ strategies: staleStrategy ? [staleStrategy] : [] })
    if (path === '/dispatch-strategies/strategy-1') return json(staleStrategy)
    if (path === '/dispatch-strategies/strategy-1/events') return json({ items: [], next_cursor: null })
    if (path === '/dispatch-strategies/strategy-1/simulate' && method === 'POST') return json({
      status: 'EVALUATED', reason_code: null, frame_sequence: 18, configuration_revision: 7,
      snapshot: {}, engine_inputs: {}, matched_rules: [], decision: {}, proposed_intents: [],
    })
    if (path === '/dispatch-strategies/strategy-1/draft' && method === 'PUT' && staleCode) {
      return json({ detail: { code: staleCode, message: 'stale' } }, 409)
    }
    if (path === '/entity-instances') return json({ items: [], total: 0 })
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
