import { expect, test, type Page, type Route } from '@playwright/test'
import { spawn, type ChildProcess } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import path from 'node:path'

import { openEngineeringPage } from './support/tabletNavigation'

const toolsPort = 4397
const toolsBaseUrl = `http://127.0.0.1:${toolsPort}`
let vite: ChildProcess | undefined

test.setTimeout(30_000)

test.beforeAll(async () => {
  vite = spawn(process.execPath, [path.resolve('node_modules/vite/bin/vite.js'), 'preview', '--host', '127.0.0.1', '--port', String(toolsPort), '--strictPort'], {
    cwd: process.cwd(),
    env: process.env,
    windowsHide: true,
  })
  await new Promise<void>((resolve, reject) => {
    let output = ''
    const timeout = setTimeout(() => reject(new Error(`tools Vite did not start: ${output}`)), 60_000)
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
      reject(new Error(`tools Vite exited ${code}: ${output}`))
    })
  })
})

test.afterAll(() => {
  vite?.kill()
})

const notification = {
  id: 'http-config-1',
  name: '值班系统',
  description: '正式测试通道',
  method: 'POST',
  url_display: 'https://receiver.invalid/***',
  query_params: [],
  headers: [],
  content_type: 'application/json',
  body_template: '{"event":{{event.type}}}',
  timeout_seconds: 5,
  current_digest: 'digest-1',
  tested_digest: null,
  tested_at: null,
  last_test_status: null,
  enabled: false,
}

type ToolsApiScenario = {
  configReadFailure?: boolean
  configWriteFailure?: boolean
  faultMapsFailure?: boolean
  healthFailure?: boolean
  healthDisconnected?: boolean
}

async function installToolsApi(page: Page, role: 'admin' | 'engineer' = 'admin', scenario: ToolsApiScenario = {}) {
  const writes: string[] = []
  page.on('pageerror', (error) => process.stderr.write(`[tools pageerror] ${error.message}\n`))
  page.on('console', (message) => {
    if (message.type() === 'error') process.stderr.write(`[tools console] ${message.text()}\n`)
  })
  await page.addInitScript(({ nextRole }) => {
    window.sessionStorage.setItem('zizu.auth.session.v1', JSON.stringify({
      accessToken: 'local-tools',
      expiresAt: '2099-01-01T00:00:00Z',
      user: { id: `${nextRole}-tools`, username: `${nextRole}-tools`, role: nextRole },
    }))
  }, { nextRole: role })
  await page.routeWebSocket('**/api/v1/ws/data-frames', (socket) => {
    socket.onMessage((message) => {
      const body = JSON.parse(String(message))
      if (body.authenticate) socket.send(JSON.stringify({ type: 'authenticated' }))
      if (body.subscribe) socket.send(JSON.stringify({ type: 'subscribed' }))
    })
  })
  await page.route('**/api/v1/**', async (route: Route) => {
    const request = route.request()
    const pathName = new URL(request.url()).pathname.replace('/api/v1', '')
    const method = request.method()
    if (method !== 'GET' && pathName !== '/auth/login') writes.push(`${method} ${pathName}`)
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
    const user = { id: `${role}-tools`, username: `${role}-tools`, role }

    if (pathName === '/auth/login') return json({ access_token: 'local-tools', expires_at: '2099-01-01T00:00:00Z', user })
    if (pathName === '/auth/me') return json({ user })
    if (pathName === '/health' && scenario.healthFailure) return json({ detail: 'HEALTH_UPSTREAM_UNAVAILABLE' }, 503)
    if (pathName === '/health') return json({
      status: 'healthy', version: 'local', uptime_seconds: 60,
      pipeline: { status: 'running', messages_received: 25, points_written_db: 24, last_message_at: '2026-09-08T10:00:00+08:00' },
      components: { timescaledb: { status: 'connected' }, mqtt: { status: scenario.healthDisconnected ? 'disconnected' : 'connected' }, neuron: { status: 'connected' } },
    })
    if (pathName === '/ems-workbench') return json({
      workbench_id: 'default', configuration_revision: 7,
      navigation: [], groups: [], kpis: [], trends: [],
      alarms: { visible: true }, controls: { visible: false, entities: [] },
    })
    if (pathName === '/auth/ws-ticket') return json({ ticket: 'isolated-tools-ui' })
    if (pathName === '/runtime/frame-snapshot') return json({
      type: 'frame_snapshot', node_id: null, cursor: 'isolated-tools', frame_sequence: 0,
      frame_time: null, configuration_revision: 7, frame_status: null,
      failure: null, backlog_frames: 0, l0: [], l2: [],
    })
    if (pathName === '/categories') return json({ categories: [] })
    if (pathName === '/alarms/counts') return json({ counts: {} })
    if (pathName === '/entity-instances') return json({ items: [], total: 0 })
    if (pathName === '/pipeline/config' && method === 'GET' && scenario.configReadFailure) return json({ detail: 'PIPELINE_READ_FORBIDDEN' }, 403)
    if (pathName === '/pipeline/config' && method === 'PUT' && scenario.configWriteFailure) return json({ detail: 'PIPELINE_REVISION_CONFLICT' }, 409)
    if (pathName === '/pipeline/config') return json({ batch_size: 50, flush_interval_sec: 1 })
    if (pathName === '/mqtt-config' && method === 'GET' && scenario.configReadFailure) return json({ detail: 'MQTT_CONFIG_UNAVAILABLE' }, 503)
    if (pathName === '/mqtt-config' && method === 'PUT' && scenario.configWriteFailure) return json({ detail: 'MQTT_CONFIG_WRITE_FORBIDDEN' }, 403)
    if (pathName === '/mqtt-config') return json({ mqtt_telemetry_topic: '/neuron/#', persisted: null, effective_topics: ['/neuron/#'] })
    if (pathName === '/admin/alarm-http-notifications' && method === 'GET') return json([notification])
    if (pathName === '/admin/alarm-http-notifications/http-config-1/test' && method === 'POST') return json({
      ...notification,
      tested_digest: 'digest-1',
      tested_at: '2026-09-08T02:03:04Z',
      last_test_status: {
        delivered: true,
        outcome: 'DELIVERED',
        http_status: 202,
        duration_ms: 37,
        error_code: null,
        error_detail: null,
        response_excerpt: 'accepted-receipt-7f2a',
      },
    })
    if (pathName === '/admin/truncate' && method === 'POST') {
      return json({ status: 'ok', table: 't_telemetry', rows_deleted: 12 })
    }
    if (pathName === '/fault-maps' && method === 'GET' && scenario.faultMapsFailure) return json({ detail: 'FAULT_MAP_SERVICE_UNAVAILABLE' }, 503)
    if (pathName === '/fault-maps') return json({ items: [], total: 0 })
    if (pathName === '/nodes') return json({ nodes: [] })
    if (pathName.startsWith('/telemetry')) return json({ points: [], has_more: false, next_cursor: null })
    if (pathName === '/nanomq/status') return json({ brokers: { data: [{ version: '0.22.10', uptime: '1h' }] }, metrics: { data: { connections: 2, subscriptions: 3 } } })
    if (pathName === '/nanomq/clients') return json({ data: [] })
    if (pathName === '/nanomq/subscriptions') return json({ data: [] })
    if (pathName === '/nanomq/acl') return json({ data: [] })
    if (pathName === '/nanomq/config') return json({ content: '', path: '/etc/nanomq.conf' })
    return json({})
  })
  return writes
}

async function login(page: Page, role: 'admin' | 'engineer' = 'admin') {
  await page.goto(toolsBaseUrl, { waitUntil: 'domcontentloaded' })
  const engineeringEntry = page.getByRole('banner').getByRole('button', { name: '工程配置', exact: true })
  await expect(engineeringEntry).toBeVisible()
}

test('系统工具以四个正式分区打开管理器且打开动作不产生写请求', async ({ page }) => {
  const writes = await installToolsApi(page)
  await login(page)
  await openEngineeringPage(page, '系统工具')

  const tools = page.getByTestId('tablet-admin-applications')
  for (const name of ['NanoMQ / MQTT', 'HTTP 通知', '故障映射', '数据与系统状态']) {
    await expect(tools.getByRole('heading', { name, exact: true })).toBeVisible()
    const open = tools.getByRole('button', { name: `打开${name}`, exact: true })
    await expect(open).toBeVisible()
    expect((await open.boundingBox())?.height).toBeGreaterThanOrEqual(44)
  }
  await expect(tools.getByRole('button', { name: /重置/ })).toHaveCount(0)
  const evidenceDirectory = path.resolve('test-results/task-8-tools')
  mkdirSync(evidenceDirectory, { recursive: true })
  for (const viewport of [{ width: 1024, height: 768 }, { width: 1280, height: 800 }]) {
    await page.setViewportSize(viewport)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
    await page.screenshot({ path: path.join(evidenceDirectory, `tools-${viewport.width}x${viewport.height}.png`), fullPage: true })
  }

  await tools.getByRole('button', { name: '打开NanoMQ / MQTT', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'NanoMQ / MQTT' })).toBeVisible()
  await page.getByRole('dialog', { name: 'NanoMQ / MQTT' }).getByRole('button', { name: '关闭', exact: true }).click()
  await tools.getByRole('button', { name: '打开HTTP 通知', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'HTTP 通知' }).getByRole('region', { name: 'HTTP 通知' })).toBeVisible()
  await page.getByRole('dialog', { name: 'HTTP 通知' }).getByRole('button', { name: '关闭', exact: true }).click()
  await tools.getByRole('button', { name: '打开故障映射', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '故障映射' }).getByRole('button', { name: '新建映射表', exact: true })).toBeVisible()
  await page.getByRole('dialog', { name: '故障映射' }).getByRole('button', { name: '关闭', exact: true }).click()
  await tools.getByRole('button', { name: '打开数据与系统状态', exact: true }).click()
  const dataDialog = page.getByRole('dialog', { name: '数据与系统状态' })
  await expect(dataDialog.getByRole('button', { name: '执行', exact: true })).toBeVisible()
  const health = dataDialog.getByRole('region', { name: '系统健康状态' })
  await expect(health).toContainText('healthy')
  await expect(health).toContainText('TimescaleDB connected')
  await expect(health).toContainText('MQTT connected')
  expect(writes).toEqual([])
})

test('非管理员没有系统工具入口且不会触发系统管理请求', async ({ page }) => {
  const writes = await installToolsApi(page, 'engineer')
  await login(page, 'engineer')
  await page.getByRole('banner').getByRole('button', { name: '工程配置', exact: true }).click()

  await expect(page.getByRole('button', { name: '系统工具', exact: true })).toHaveCount(0)
  expect(writes).toEqual([])
})

test('清空表先显示范围和不可恢复后果，确认前不发送请求', async ({ page }) => {
  const writes = await installToolsApi(page)
  await login(page)
  await openEngineeringPage(page, '系统工具')
  await page.getByRole('button', { name: '打开数据与系统状态', exact: true }).click()
  const toolsDialog = page.getByRole('dialog', { name: '数据与系统状态' })

  await toolsDialog.getByRole('button', { name: '准备清空表', exact: true }).click()
  const danger = page.getByRole('dialog', { name: '确认永久清空数据' })
  await expect(danger).toContainText('t_telemetry')
  await expect(danger).toContainText('不可恢复')
  expect(writes).toEqual([])
  await danger.getByLabel('输入 yes 确认').fill('yes')
  await danger.getByRole('button', { name: '永久清空', exact: true }).click()
  await expect(toolsDialog.getByRole('status')).toContainText('已清空 t_telemetry，删除 12 行')
  expect(writes).toEqual(['POST /admin/truncate'])
})

test('HTTP 测试展示服务端返回的正式收据而非通用成功文案', async ({ page }) => {
  const writes = await installToolsApi(page)
  await login(page)
  await openEngineeringPage(page, '系统工具')
  await page.getByRole('button', { name: '打开HTTP 通知', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'HTTP 通知' })
  const card = dialog.locator('article').filter({ hasText: '值班系统' })

  await card.getByRole('button', { name: '发送测试', exact: true }).click()
  const receipt = card.getByRole('status', { name: 'HTTP 测试收据' })
  await expect(receipt).toContainText('DELIVERED')
  await expect(receipt).toContainText('HTTP 202')
  await expect(receipt).toContainText('37 ms')
  await expect(receipt).toContainText('accepted-receipt-7f2a')
  await expect(receipt).toContainText('2026/9/8')
  expect(writes).toEqual(['POST /admin/alarm-http-notifications/http-config-1/test'])
})

test('配置读取失败显示服务端错误且不暴露可写默认值', async ({ page }) => {
  const writes = await installToolsApi(page, 'admin', { configReadFailure: true })
  await login(page)
  await openEngineeringPage(page, '系统工具')
  await page.getByRole('button', { name: '打开NanoMQ / MQTT', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'NanoMQ / MQTT' })

  const pipeline = dialog.getByRole('region', { name: 'Pipeline 配置' })
  await expect(pipeline.getByRole('alert')).toContainText('PIPELINE_READ_FORBIDDEN')
  await expect(pipeline.getByRole('button', { name: '保存配置', exact: true })).toHaveCount(0)
  await expect(pipeline.getByRole('spinbutton')).toHaveCount(0)

  const mqtt = dialog.getByRole('region', { name: 'MQTT 北向主题' })
  await expect(mqtt.getByRole('alert')).toContainText('MQTT_CONFIG_UNAVAILABLE')
  await expect(mqtt.getByRole('button', { name: '保存并重订阅', exact: true })).toHaveCount(0)
  await expect(mqtt.getByRole('textbox')).toHaveCount(0)
  expect(writes).toEqual([])
})

test('配置保存失败保留服务端错误类别', async ({ page }) => {
  const writes = await installToolsApi(page, 'admin', { configWriteFailure: true })
  await login(page)
  await openEngineeringPage(page, '系统工具')
  await page.getByRole('button', { name: '打开NanoMQ / MQTT', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'NanoMQ / MQTT' })

  await dialog.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(dialog.getByRole('region', { name: 'Pipeline 配置' }).getByRole('alert')).toContainText('PIPELINE_REVISION_CONFLICT')
  await dialog.getByRole('button', { name: '保存并重订阅', exact: true }).click()
  await expect(dialog.getByRole('region', { name: 'MQTT 北向主题' }).getByRole('alert')).toContainText('MQTT_CONFIG_WRITE_FORBIDDEN')
  expect(writes).toEqual(['PUT /pipeline/config', 'PUT /mqtt-config'])
})

test('系统健康接口失败明确显示连接中断并允许重试', async ({ page }) => {
  await installToolsApi(page, 'admin', { healthFailure: true })
  await login(page)
  await openEngineeringPage(page, '系统工具')
  await page.getByRole('button', { name: '打开数据与系统状态', exact: true }).click()
  const failed = page.getByRole('dialog', { name: '数据与系统状态' }).getByRole('region', { name: '系统健康状态' })
  await expect(failed.getByRole('alert')).toContainText('HEALTH_UPSTREAM_UNAVAILABLE')
  await expect(failed).toContainText('连接中断')
  await expect(failed.getByRole('button', { name: '重试系统状态', exact: true })).toBeVisible()
})

test('系统组件断线保留服务端状态而不显示健康', async ({ page }) => {
  await installToolsApi(page, 'admin', { healthDisconnected: true })
  await login(page)
  await openEngineeringPage(page, '系统工具')
  await page.getByRole('button', { name: '打开数据与系统状态', exact: true }).click()
  const disconnected = page.getByRole('dialog', { name: '数据与系统状态' }).getByRole('region', { name: '系统健康状态' })
  await expect(disconnected).toContainText('连接异常')
  await expect(disconnected).toContainText('MQTT disconnected')
})

test('故障映射读取失败显示错误和重试，不伪装成空库', async ({ page }) => {
  const pageErrors: string[] = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  await installToolsApi(page, 'admin', { faultMapsFailure: true })
  await login(page)
  await openEngineeringPage(page, '系统工具')
  await page.getByRole('button', { name: '打开故障映射', exact: true }).click()
  const manager = page.getByRole('dialog', { name: '故障映射' }).getByRole('region', { name: '故障映射管理' })

  await expect(manager.getByRole('alert')).toContainText('FAULT_MAP_SERVICE_UNAVAILABLE')
  await expect(manager.getByRole('button', { name: '重试故障映射', exact: true })).toBeVisible()
  await expect(manager.getByText('暂无故障码映射表', { exact: true })).toHaveCount(0)
  expect(pageErrors).toEqual([])
})
