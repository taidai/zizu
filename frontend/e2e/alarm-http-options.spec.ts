import { expect, test, type Page } from '@playwright/test'

const optionsPath = '**/api/v1/alarm-http-notification-options'
test.use({ actionTimeout: 5000 })
test.setTimeout(15000)

test.beforeEach(async ({ page, baseURL }) => {
  test.skip(!baseURL || !['localhost', '127.0.0.1'].includes(new URL(baseURL).hostname), 'Synthetic local test only')
  const user = { id: 'options-user', username: 'options-test', role: 'engineer' }
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/auth/login') {
      await route.fulfill({ json: { access_token: 'local-test', expires_at: '2099-01-01T00:00:00Z', user } })
    } else if (path === '/api/v1/auth/me') {
      await route.fulfill({ json: { user } })
    } else if (['/api/v1/alarm-rule-groups', '/api/v1/alarm-rule-sets', '/api/v1/alarms/entities', '/api/v1/alarm-events'].includes(path)) {
      await route.fulfill({ json: { items: [], total: 0, summary: { active: 0, unacknowledged: 0, critical: 0 } } })
    } else if (path === '/api/v1/entity-instances') {
      await route.fulfill({ json: { items: [{ id: 'power', data_type: 'FLOAT', display_name: '有功功率', node_display_name: '测试 PCS', unit: 'kW' }], total: 1 } })
    } else if (path === '/api/v1/health') {
      await route.fulfill({ json: {
        version: 'test', status: 'healthy', uptime_seconds: 1,
        pipeline: { status: 'running', messages_received: 0, points_written_db: 0, last_message_at: null },
        components: { mqtt: { status: 'connected' }, database: { status: 'connected' } },
      } })
    } else {
      await route.fulfill({ status: 503, json: { detail: 'Unrelated local test service is offline' } })
    }
  })
})

async function openRules(page: Page) {
  await page.goto('/')
  const login = page.getByRole('button', { name: '登录', exact: true })
  const navigation = page.getByRole('button', { name: '告警中心', exact: true })
  await Promise.race([login.waitFor({ state: 'visible' }), navigation.waitFor({ state: 'visible' })])
  if (await login.isVisible()) {
    await page.getByLabel('用户名', { exact: true }).fill('options-test')
    await page.getByLabel('密码', { exact: true }).fill('local-only')
    await login.click()
  }
  await page.getByRole('button', { name: '告警中心', exact: true }).click()
  await page.getByRole('button', { name: '告警规则', exact: true }).click()
}

test('explains disabled and untested configurations instead of silently hiding all choices', async ({ page }) => {
  await page.route(optionsPath, (route) => route.fulfill({ json: [
    { id: 'disabled', name: '飞书告警通知', status: 'disabled' },
    { id: 'untested', name: '备用通知', status: 'needs_test' },
  ] }))
  await openRules(page)
  await expect(page.getByText('飞书告警通知：已停用', { exact: true })).toBeVisible({ timeout: 2500 })
  await expect(page.getByText('备用通知：需测试并启用', { exact: true })).toBeVisible()
  await expect(page.getByText(/系统工具 → HTTP 通知/)).toBeVisible()
  await expect(page.getByRole('combobox', { name: 'HTTP 通知（可选）', exact: true }).locator('option')).toHaveCount(1)
})

test('engineer can refresh and select a ready notification without losing the draft or reading admin secrets', async ({ page }) => {
  let ready = false
  const adminReads: string[] = []
  const writes: string[] = []
  page.on('request', (request) => {
    if (request.url().includes('/admin/alarm-http-notifications')) adminReads.push(request.url())
    if (request.method() !== 'GET' && !request.url().includes('/auth/')) writes.push(request.url())
  })
  await page.route(optionsPath, (route) => route.fulfill({ json: [{ id: 'feishu', name: '飞书告警通知', status: ready ? 'available' : 'disabled' }] }))
  await openRules(page)
  await page.getByLabel('规则名称', { exact: true }).fill('未保存的规则')
  await page.getByRole('checkbox', { name: /有功功率/ }).check()
  ready = true
  await page.getByRole('button', { name: '刷新通知选项', exact: true }).click()
  const select = page.getByRole('combobox', { name: 'HTTP 通知（可选）', exact: true })
  await expect(select.locator('option[value="feishu"]')).toHaveText('飞书告警通知')
  await select.selectOption('feishu')
  await expect(page.getByLabel('规则名称', { exact: true })).toHaveValue('未保存的规则')
  await expect(page.getByRole('checkbox', { name: /有功功率/ })).toBeChecked()
  expect(adminReads).toEqual([])
  expect(writes).toEqual([])
  ready = false
  await page.getByRole('button', { name: '刷新通知选项', exact: true }).click()
  await expect(page.getByText('飞书告警通知：已停用', { exact: true })).toBeVisible()
  await expect(select).toHaveValue('feishu')
  await expect(select.locator('option:checked')).toContainText('当前不可用')
})

test('failed loading is an explicit retryable error, not an empty configuration list', async ({ page }) => {
  let failed = true
  await page.route(optionsPath, (route) => route.fulfill(failed
    ? { status: 503, json: { detail: 'offline' } }
    : { json: [{ id: 'feishu', name: '飞书告警通知', status: 'available' }] }))
  await openRules(page)
  await expect(page.getByRole('alert').filter({ hasText: 'HTTP 通知选项读取失败' })).toBeVisible({ timeout: 2500 })
  await expect(page.getByText(/尚未配置 HTTP 通知/)).toBeHidden()
  failed = false
  await page.getByRole('button', { name: '刷新通知选项', exact: true }).click()
  const select = page.getByRole('combobox', { name: 'HTTP 通知（可选）', exact: true })
  await select.selectOption('feishu')
  failed = true
  await page.getByRole('button', { name: '刷新通知选项', exact: true }).click()
  await expect(page.getByRole('alert').filter({ hasText: 'HTTP 通知选项读取失败' })).toBeVisible()
  await expect(select).toHaveValue('feishu')
})

test('empty state explains where to create a notification', async ({ page }) => {
  await page.route(optionsPath, (route) => route.fulfill({ json: [] }))
  await openRules(page)
  await expect(page.getByText(/尚未配置 HTTP 通知/)).toBeVisible({ timeout: 2500 })
  await expect(page.getByText(/系统工具 → HTTP 通知/)).toBeVisible()
})
