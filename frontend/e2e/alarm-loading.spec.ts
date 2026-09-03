import { expect, test, type Page } from '@playwright/test'

// Synthetic HTTP data only; this test never touches station data or sends alarms.
test.beforeEach(async ({ page, baseURL }) => {
  test.skip(!baseURL || !['localhost', '127.0.0.1'].includes(new URL(baseURL).hostname), 'Local frontend test only')
  const user = { id: 'test-user', username: 'loading-test', role: 'admin' }
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/auth/login') {
      await route.fulfill({ json: { access_token: 'test-session', expires_at: '2099-01-01T00:00:00Z', user } })
    } else if (path === '/api/v1/auth/me') {
      await route.fulfill({ json: { user } })
    } else if (path === '/api/v1/alarms/entities') {
      await route.fulfill({ json: { items: [] } })
    } else if (path === '/api/v1/health') {
      await route.fulfill({ json: {
        version: 'test', status: 'healthy', uptime_seconds: 1,
        pipeline: { status: 'running', messages_received: 0, points_written_db: 0, last_message_at: null },
        components: { mqtt: { status: 'connected' }, database: { status: 'connected' } },
      } })
    } else {
      await route.fulfill({ status: 503, json: { detail: 'Unrelated service is intentionally offline' } })
    }
  })
})

const alarmResponse = (name: string, state = 'active_unacknowledged') => ({
  items: [{
    id: 'test-event', definition_id: 'test-definition', entity_instance_id: 'test-entity',
    state, severity: 'WARNING', pending_at: '2026-09-01T00:00:00Z',
    active_at: '2026-09-01T00:00:00Z', acknowledged_at: null, acknowledged_by: null,
    acknowledgement_note: null, recovered_at: state === 'recovered' ? '2026-09-02T00:00:00Z' : null,
    node_name: 'PCS test', entity_name: 'Power test', alarm_name: name, duration_seconds: 5,
    model_version: 'v1',
  }],
  total: 1, page: 1, page_size: 50, total_pages: 1,
  summary: { active: 1, unacknowledged: 1, critical: 0 }, model_version: 'v1',
})

async function openAlarms(page: Page) {
  await page.goto('/')
  const login = page.getByRole('button', { name: '登录', exact: true })
  const navigation = page.getByRole('button', { name: '告警中心', exact: true })
  await Promise.race([login.waitFor({ state: 'visible' }), navigation.waitFor({ state: 'visible' })])
  if (await login.isVisible()) {
    await page.getByLabel('用户名', { exact: true }).fill('loading-test')
    await page.getByLabel('密码', { exact: true }).fill('local-only')
    await login.click()
  }
  await page.getByRole('button', { name: '告警中心', exact: true }).click()
}

test('background refresh keeps the loaded alarm visible while the response is pending', async ({ page }) => {
  let hold = false
  let pending = 0
  let release!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })
  await page.route('**/api/v1/alarm-events?*', async (route) => {
    if (hold) { pending++; await gate }
    await route.fulfill({ json: alarmResponse('PCS fault remains visible') })
  })
  await openAlarms(page)
  const alarm = page.getByRole('heading', { name: 'PCS fault remains visible', exact: true })
  await expect(alarm).toBeVisible()
  await page.getByRole('checkbox', { name: '自动刷新 (5s)' }).uncheck()
  await page.clock.install()
  hold = true
  await page.getByRole('checkbox', { name: '自动刷新 (5s)' }).check()
  await page.clock.runFor(5100)
  await expect.poll(() => pending, { timeout: 2000 }).toBe(1)
  try {
    await expect(alarm).toBeVisible({ timeout: 1000 })
    await page.clock.runFor(15000)
    expect(pending).toBe(1)
  } finally {
    release()
  }
})

test('a late response cannot replace the selected alarm filter', async ({ page }) => {
  let holdOpen = false
  let release!: () => void
  const gate = new Promise<void>((resolve) => { release = resolve })
  await page.route('**/api/v1/alarm-events?*', async (route) => {
    const state = new URL(route.request().url()).searchParams.get('state')
    if (state === 'open' && holdOpen) await gate
    await route.fulfill({ json: alarmResponse(state === 'recovered' ? 'Recovered result' : 'Open result', state === 'recovered' ? 'recovered' : 'active_unacknowledged') })
  })
  await openAlarms(page)
  await expect(page.getByRole('heading', { name: 'Open result', exact: true })).toBeVisible()
  await page.getByRole('checkbox', { name: '自动刷新 (5s)' }).uncheck()
  await page.clock.install()
  holdOpen = true
  await page.getByRole('checkbox', { name: '自动刷新 (5s)' }).check()
  await page.clock.runFor(5100)
  await page.getByRole('button', { name: '已恢复', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Recovered result', exact: true })).toBeVisible()
  release()
  await page.getByRole('checkbox', { name: '自动刷新 (5s)' }).uncheck()
  await expect(page.getByRole('heading', { name: 'Open result', exact: true })).toBeHidden()
})
