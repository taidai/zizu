import { expect, test, type Page, type Route } from '@playwright/test'

const controlEntity = {
  entity_instance_id: 'entity-control', node_id: 'node-pcs', node_name: '1# PCS',
  definition_id: 'pcs.power_setpoint', display_name: 'PCS 功率设定', data_type: 'float', unit: 'kW',
  direction: 'RW', status: 'available', value: 0, quality: 192, observed_at: '2026-09-08T08:00:00Z',
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installControlFixture(page: Page) {
  const user = { id: 'operator-1', username: 'control-operator', role: 'operator' }
  const calls = { confirmations: [] as Array<{ body: unknown; key: string | null }>, commands: [] as Array<{ body: unknown; key: string | null }>, reconcile: 0, reads: 0, slotWrites: 0 }
  await page.addInitScript((currentUser) => {
    sessionStorage.setItem('zizu.auth.session.v1', JSON.stringify({ accessToken: 'control-token', expiresAt: '2099-01-01T00:00:00Z', user: currentUser }))
  }, user)
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.endsWith('/auth/me')) return json(route, { user })
    if (url.pathname.endsWith('/health')) return json(route, { status: 'healthy', version: 'test', uptime_seconds: 1, components: { timescaledb: { status: 'connected' }, mqtt: { status: 'connected' }, neuron: { status: 'connected' } }, pipeline: { status: 'running', messages_received: 1, points_written_db: 1, last_message_at: '2026-09-08T08:00:00Z' } })
    if (url.pathname.endsWith('/ems-workbench')) return json(route, { workbench_id: 'fixed-light-storage-charging', configuration_revision: 12, navigation: [], groups: [], kpis: [], trends: [], alarms: { visible: true }, controls: { visible: true, entities: [controlEntity] } })
    if (url.pathname.endsWith('/entity-instances')) return json(route, { items: [], total: 0 })
    if (url.pathname.endsWith('/alarms/counts')) return json(route, { counts: {} })
    if (url.pathname.endsWith('/alarm-events')) return json(route, { items: [], total: 0, page: 1, page_size: 10, total_pages: 1, summary: { active: 0, unacknowledged: 0, critical: 0 } })
    if (url.pathname.endsWith('/dispatch-strategies')) return json(route, { strategies: [] })
    if (url.pathname.includes('/ems-workbench/slots/')) { calls.slotWrites += 1; return json(route, {}) }
    if (url.pathname.endsWith('/entity-instances/entity-control/control-confirmations')) {
      calls.confirmations.push({ body: request.postDataJSON(), key: await request.headerValue('Idempotency-Key') })
      return json(route, { id: 'confirmation-1', expires_at: '2099-01-01T00:00:00Z' }, 201)
    }
    if (url.pathname.endsWith('/entity-instances/entity-control/control-commands')) {
      calls.commands.push({ body: request.postDataJSON(), key: await request.headerValue('Idempotency-Key') })
      return json(route, { id: 'command-1', status: 'dispatched', code: 'CONTROL_DISPATCHED', source_type: 'manual', timeout_at: '2099-01-01T00:00:00Z' }, 201)
    }
    if (url.pathname.endsWith('/control-commands/command-1/reconcile')) {
      calls.reconcile += 1
      return json(route, { id: 'command-1', status: calls.reconcile > 1 ? 'readback_confirmed' : 'dispatched', code: calls.reconcile > 1 ? 'CONTROL_READBACK_CONFIRMED' : 'CONTROL_READBACK_PENDING_MISMATCH', source_type: 'manual' })
    }
    if (url.pathname.endsWith('/control-commands/command-1')) {
      calls.reads += 1
      return json(route, { id: 'command-1', status: 'dispatched', code: 'CONTROL_READBACK_PENDING_MISMATCH', source_type: 'manual' })
    }
    return json(route, { items: [], total: 0 })
  })
  return calls
}

test('manual control confirms once, dispatches once, then reconciles and queries committed readback', async ({ page }, testInfo) => {
  const calls = await installControlFixture(page)
  await page.setViewportSize({ width: 1024, height: 768 })
  await page.goto('/', { waitUntil: 'networkidle' })
  await page.getByRole('navigation', { name: '日常运行' }).getByRole('button', { name: '手动控制' }).click()

  await expect(page.getByRole('heading', { name: '手动控制', exact: true })).toBeVisible()
  await page.getByLabel('PCS 功率设定目标值').fill('26.8')
  await page.getByRole('button', { name: '申请二次确认' }).evaluate((button: HTMLButtonElement) => { button.click(); button.click() })
  await expect(page.getByRole('alertdialog', { name: '确认控制命令' })).toBeVisible()
  expect(calls.confirmations).toHaveLength(1)
  expect(calls.confirmations[0].body).toEqual({ value: 26.8 })
  expect(calls.confirmations[0].key).toBeTruthy()

  await page.getByRole('button', { name: '确认下发' }).evaluate((button: HTMLButtonElement) => { button.click(); button.click() })
  await expect(page.getByRole('status').filter({ hasText: '等待设备回读' })).toBeVisible()
  expect(calls.commands).toHaveLength(1)
  expect(calls.commands[0].body).toEqual({ value: 26.8, confirmation_id: 'confirmation-1' })
  expect(calls.commands[0].key).toBeTruthy()
  expect(calls.reconcile).toBe(1)
  await expect.poll(() => calls.reads).toBe(1)
  expect(calls.slotWrites).toBe(0)

  await page.getByRole('button', { name: '刷新回读' }).click()
  await expect(page.getByRole('status').filter({ hasText: '回读已确认' })).toBeVisible()
  expect(calls.commands).toHaveLength(1)
  expect(calls.reconcile).toBe(2)
  await page.screenshot({ path: testInfo.outputPath('manual-control-1024x768.png'), fullPage: true })
})

test('expired confirmation fails closed in place without a command write', async ({ page }) => {
  const calls = await installControlFixture(page)
  await page.route('**/api/v1/entity-instances/entity-control/control-confirmations', (route) => json(route, { id: 'expired-confirmation', expires_at: '2000-01-01T00:00:00Z' }, 201))
  await page.goto('/', { waitUntil: 'networkidle' })
  await page.getByRole('navigation', { name: '日常运行' }).getByRole('button', { name: '手动控制' }).click()
  await page.getByLabel('PCS 功率设定目标值').fill('10')
  await page.getByRole('button', { name: '申请二次确认' }).click()
  await expect(page.getByRole('alertdialog', { name: '确认控制命令' })).toContainText('已过期')
  await expect(page.getByRole('button', { name: '确认下发' })).toBeDisabled()
  expect(calls.commands).toHaveLength(0)
})

test('confirmation errors stay local and do not look like a successful or zero-valued write', async ({ page }) => {
  const calls = await installControlFixture(page)
  await page.route('**/api/v1/entity-instances/entity-control/control-confirmations', (route) => json(route, { detail: { code: 'CONTROL_INPUT_STALE', message: '控制输入质量或新鲜度不满足' } }, 409))
  await page.goto('/', { waitUntil: 'networkidle' })
  await page.getByRole('navigation', { name: '日常运行' }).getByRole('button', { name: '手动控制' }).click()
  await page.getByLabel('PCS 功率设定目标值').fill('12')
  await page.getByRole('button', { name: '申请二次确认' }).click()
  await expect(page.getByRole('alert')).toContainText('控制输入质量或新鲜度不满足')
  await expect(page.getByText('回读已确认')).toHaveCount(0)
  expect(calls.commands).toHaveLength(0)
})
