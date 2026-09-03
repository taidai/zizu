import { expect, test, type Locator } from '@playwright/test'

test.use({ actionTimeout: 5000 })
test.setTimeout(15000)

test.beforeEach(async ({ page, baseURL }) => {
  test.skip(!baseURL || !['localhost', '127.0.0.1'].includes(new URL(baseURL).hostname), 'Synthetic local test only')
  const user = { id: 'cursor-user', username: 'cursor-test', role: 'admin' }
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/auth/login') {
      await route.fulfill({ json: { access_token: 'local-test', expires_at: '2099-01-01T00:00:00Z', user } })
    } else if (path === '/api/v1/auth/me') {
      await route.fulfill({ json: { user } })
    } else if (path === '/api/v1/admin/alarm-http-notifications') {
      await route.fulfill({ json: [] })
    } else if (path === '/api/v1/mqtt-config') {
      await route.fulfill({ json: { mqtt_telemetry_topic: '/neuron/#', persisted: null, effective_topics: [] } })
    } else if (path === '/api/v1/pipeline/config') {
      await route.fulfill({ json: { batch_size: 50, flush_interval_sec: 1 } })
    } else if (path === '/api/v1/fault-maps') {
      await route.fulfill({ json: { items: [], total: 0 } })
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
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  const login = page.getByRole('button', { name: '登录', exact: true })
  const navigation = page.getByRole('button', { name: '系统工具', exact: true })
  await Promise.race([login.waitFor({ state: 'visible' }), navigation.waitFor({ state: 'visible' })])
  if (await login.isVisible()) {
    await page.getByLabel('用户名', { exact: true }).fill('cursor-test')
    await page.getByLabel('密码', { exact: true }).fill('local-only')
    await login.click()
  }
  await navigation.click()
  await page.getByRole('region', { name: 'HTTP 通知' }).getByRole('button', { name: '新增通知', exact: true }).click()
})

async function moveFromStart(editor: Locator, characters: number) {
  await editor.press('Control+Home')
  for (let index = 0; index < characters; index += 1) await editor.press('ArrowRight')
}

test('inserts a variable at the caret within Chinese body text, not at the end', async ({ page }) => {
  const panel = page.getByRole('region', { name: 'HTTP 通知' })
  const body = panel.getByRole('textbox', { name: '请求体模板', exact: true })
  await body.fill('前缀后缀')
  await moveFromStart(body, 2)
  await panel.getByRole('button', { name: '{{alarm.name}}', exact: true }).click()
  await expect(body).toHaveValue('前缀{{alarm.name}}后缀', { timeout: 1500 })
})

test('replaces the selected Chinese text in a multiline body', async ({ page }) => {
  const panel = page.getByRole('region', { name: 'HTTP 通知' })
  const body = panel.getByRole('textbox', { name: '请求体模板', exact: true })
  await body.fill('第一行\n前缀待替换后缀\n最后一行')
  await moveFromStart(body, 6)
  for (let index = 0; index < 3; index += 1) await body.press('Shift+ArrowRight')
  await panel.getByRole('button', { name: '{{node.name}}', exact: true }).click()
  await expect(body).toHaveValue('第一行\n前缀{{node.name}}后缀\n最后一行', { timeout: 1500 })
})

test('keeps the caret after each inserted variable so consecutive clicks and typing preserve the suffix', async ({ page }) => {
  const panel = page.getByRole('region', { name: 'HTTP 通知' })
  const body = panel.getByRole('textbox', { name: '请求体模板', exact: true })
  await body.fill('前😀后')
  await moveFromStart(body, 2)
  await panel.getByRole('button', { name: '{{alarm.name}}', exact: true }).click()
  await expect(body).toBeFocused({ timeout: 1500 })
  await panel.getByRole('button', { name: '{{node.name}}', exact: true }).click()
  await expect(body).toBeFocused()
  await page.keyboard.insertText('继续')
  await expect(body).toHaveValue('前😀{{alarm.name}}{{node.name}}继续后')
})

for (const scenario of [
  { name: 'the beginning', original: '告警', position: 0, expected: '{{event.type}}告警' },
  { name: 'the end', original: '告警', position: 2, expected: '告警{{event.type}}' },
  { name: 'an empty body', original: '', position: 0, expected: '{{event.type}}' },
]) {
  test(`inserts into ${scenario.name} and leaves the editor ready for typing`, async ({ page }) => {
    const panel = page.getByRole('region', { name: 'HTTP 通知' })
    const body = panel.getByRole('textbox', { name: '请求体模板', exact: true })
    await body.fill(scenario.original)
    await moveFromStart(body, scenario.position)
    await panel.getByRole('button', { name: '{{event.type}}', exact: true }).click()
    await expect(body).toHaveValue(scenario.expected)
    await expect(body).toBeFocused()
  })
}

test('keyboard activation restores the caret even when replacing a variable with itself', async ({ page }) => {
  const panel = page.getByRole('region', { name: 'HTTP 通知' })
  const body = panel.getByRole('textbox', { name: '请求体模板', exact: true })
  await body.fill('{{alarm.name}}')
  await body.press('Control+A')
  for (let index = 0; index < 3; index += 1) await page.keyboard.press('Tab')
  await expect(panel.getByRole('button', { name: '{{alarm.name}}', exact: true })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(body).toBeFocused()
  await page.keyboard.insertText('后缀')
  await expect(body).toHaveValue('{{alarm.name}}后缀')
})

test('uses the retained body caret after editing the URL without changing or sending request fields', async ({ page }) => {
  const writes: string[] = []
  page.on('request', (request) => {
    if (request.method() !== 'GET') writes.push(new URL(request.url()).pathname)
  })
  const panel = page.getByRole('region', { name: 'HTTP 通知' })
  const body = panel.getByRole('textbox', { name: '请求体模板', exact: true })
  const url = panel.getByRole('textbox', { name: '请求地址', exact: true })
  await body.fill('前缀后缀')
  await moveFromStart(body, 2)
  await url.fill('https://example.invalid/保持地址')
  await panel.getByRole('button', { name: '{{entity.name}}', exact: true }).click()
  await expect(body).toHaveValue('前缀{{entity.name}}后缀')
  await expect(url).toHaveValue('https://example.invalid/保持地址')
  await expect(body).toBeFocused()
  expect(writes).toEqual([])
})
