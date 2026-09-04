import { expect, test, type Page } from '@playwright/test'

test.use({ actionTimeout: 4000 })
test.setTimeout(15000)

const formalRule = {
  id: 'fault', name: '电源故障', severity: 'WARNING',
  trigger: { operator: 'eq', value: true }, recovery: { operator: 'eq', value: false },
  trigger_duration_seconds: 0, recovery_duration_seconds: 3, notification_throttle_seconds: 60,
  unit: null, fault_map_id: null, http_notification_config_id: 'notice',
}

async function fixture(page: Page, enabled: boolean, published: number | null = 1) {
  const revisions = [
    { rule_set_id: 'group', key: 'fault', name: '电源保护', revision: 1, rules: [formalRule], digest: 'formal' },
    { rule_set_id: 'group', key: 'fault', name: '电源保护', revision: 2, rules: [{ ...formalRule, trigger: { operator: 'eq', value: false }, recovery: { operator: 'eq', value: true } }], digest: 'draft' },
  ]
  const writes: { path: string; body: any }[] = []
  const user = { id: 'test-user', username: 'toggle-test', role: 'engineer' }
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() !== 'GET' && !path.includes('/auth/')) writes.push({ path, body: request.postDataJSON() })
    if (path === '/api/v1/auth/login') return route.fulfill({ json: { access_token: 'local-test', user, expires_at: '2099-01-01T00:00:00Z' } })
    if (path === '/api/v1/auth/me') return route.fulfill({ json: { user } })
    if (path === '/api/v1/alarm-rule-groups') return route.fulfill({ json: { items: [{
      rule_set_id: 'group', key: 'fault', name: '电源保护', latest_revision: 2, last_non_empty_revision: 2,
      last_published_revision: published, entity_instance_ids: ['entity'], enabled_entity_instance_ids: enabled ? ['entity'] : [],
      device_count: 1, rule_count: 1, highest_severity: 'WARNING',
    }] } })
    if (path === '/api/v1/alarm-rule-sets') return route.fulfill({ json: { items: revisions } })
    if (path === '/api/v1/entity-instances') return route.fulfill({ json: { items: [{ id: 'entity', data_type: 'BOOL', display_name: '电源故障', node_display_name: '测试设备', unit: null }], total: 1 } })
    if (path === '/api/v1/alarm-http-notification-options') return route.fulfill({ json: [{ id: 'notice', name: '值班通知', status: 'available' }] })
    if (path === '/api/v1/alarm-rule-sets/group/revisions') return route.fulfill({ status: 201, json: { ...revisions[0], revision: 3, rules: [], digest: 'stop' } })
    if (path === '/api/v1/alarm-configuration-plans') return route.fulfill({ json: { id: 'plan', digest: 'plan-digest', status: 'ready', items: [], blockers: [] } })
    if (path === '/api/v1/alarm-configuration-plans/plan/apply') {
      enabled = !enabled
      return route.fulfill({ json: { id: 'applied', configuration_revision: 42 } })
    }
    if (path === '/api/v1/alarm-events' || path === '/api/v1/alarms/entities') return route.fulfill({ json: { items: [], total: 0, summary: { active: 0, unacknowledged: 0, critical: 0 } } })
    if (path === '/api/v1/health') return route.fulfill({ json: {
      version: 'test', status: 'healthy', uptime_seconds: 1,
      pipeline: { status: 'running', messages_received: 0, points_written_db: 0, last_message_at: null },
      components: { mqtt: { status: 'connected' }, database: { status: 'connected' } },
    } })
    return route.fulfill({ status: 503, json: { detail: 'Unrelated local service' } })
  })
  await page.goto('/')
  const login = page.getByRole('button', { name: '登录', exact: true })
  const navigation = page.getByRole('button', { name: '告警中心', exact: true })
  await Promise.race([login.waitFor({ state: 'visible' }), navigation.waitFor({ state: 'visible' })])
  if (await login.isVisible()) {
    await page.getByLabel('用户名', { exact: true }).fill('toggle-test')
    await page.getByLabel('密码', { exact: true }).fill('local-only')
    await login.click()
  }
  await navigation.click()
  await page.getByRole('button', { name: '告警规则', exact: true }).click()
  return writes
}

test.beforeEach(async ({ baseURL }) => {
  test.skip(!baseURL || !['localhost', '127.0.0.1'].includes(new URL(baseURL).hostname), 'Local synthetic test only')
})

test('reenable publishes the formally applied version rather than the saved draft', async ({ page }) => {
  const writes = await fixture(page, false)
  await page.getByRole('button', { name: '启用', exact: true }).click()
  await expect(page.getByRole('button', { name: '停用', exact: true })).toBeVisible()
  expect(writes.find((item) => item.path === '/api/v1/alarm-configuration-plans')?.body.rule_set_revision).toBe(1)
  expect(writes.some((item) => item.path.includes('/revisions'))).toBe(false)
})

test('disable then reenable across a reload preserves formal conditions and leaves the draft unchanged', async ({ page }) => {
  const writes = await fixture(page, true)
  await page.getByRole('button', { name: '停用', exact: true }).click()
  await expect(page.getByRole('button', { name: '启用', exact: true })).toBeVisible()
  await page.reload()
  await page.getByRole('button', { name: '告警中心', exact: true }).click()
  await page.getByRole('button', { name: '告警规则', exact: true }).click()
  await page.getByRole('button', { name: '启用', exact: true }).click()
  await expect(page.getByRole('button', { name: '停用', exact: true })).toBeVisible()
  expect(writes.filter((item) => item.path === '/api/v1/alarm-configuration-plans').map((item) => item.body.rule_set_revision)).toEqual([3, 1])
  expect(writes.filter((item) => item.path.endsWith('/revisions')).map((item) => item.body.rules)).toEqual([[]])
})

test('no unique published version blocks enable without writing any configuration', async ({ page }) => {
  const writes = await fixture(page, false, null)
  await expect(page.getByRole('button', { name: '启用', exact: true })).toBeDisabled()
  await expect(page.getByText(/没有可直接恢复的正式配置/)).toBeVisible()
  expect(writes).toEqual([])
})

test('shows formal conditions separately from an unpublished draft and explains continuous alarm notifications', async ({ page }) => {
  await fixture(page, true)
  const summary = page.getByTestId('alarm-group-formal-group')
  await expect(summary).toContainText('当前生效')
  await expect(summary).toContainText('= true')
  await expect(summary).toContainText('= false')
  await expect(page.getByText(/另有未发布草稿/)).toBeVisible()
  await expect(page.getByText(/同一次未恢复告警不会重复发送/)).toBeVisible()
})
