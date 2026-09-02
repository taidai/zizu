import {
  expect,
  test,
  type APIRequestContext,
  type BrowserContext,
  type Page,
} from '@playwright/test'

import { buildAcceptanceEnvironment } from './support/acceptanceEnvironment.mjs'
import {
  alarmHttpReceiverStatus,
  runAlarmHttpFixture,
  setupAlarmHttpFixture,
  type AlarmHttpFixtureSetup,
} from './support/alarmHttpNotificationFixture'
import { publishRawPoint } from './support/e2eFixture'

type ApiSession = { token: string }
type Delivery = {
  id: string
  event_type: string | null
  alarm_name: string | null
  status: string
}

test.describe.serial('告警 HTTP 通知闭环', () => {
  const environment = buildAcceptanceEnvironment(process.env)
  let context: BrowserContext
  let page: Page
  let fixture: AlarmHttpFixtureSetup
  let apiSession: ApiSession
  let notificationConfigId = ''

  test.beforeAll(async ({ browser, request }) => {
    fixture = await setupAlarmHttpFixture(environment)
    await runAlarmHttpFixture('start-receiver', environment)
    await runAlarmHttpFixture('clear-receiver', environment)
    apiSession = await loginApi(request)
    context = await browser.newContext({ baseURL: environment.baseUrl })
    page = await context.newPage()
    await loginPage(page)
  })

  test.afterAll(async () => {
    try {
      await runAlarmHttpFixture('cleanup', environment)
    } finally {
      await context?.close()
    }
  })

  test('界面可创建、测试并启用 HTTP 请求', async () => {
    test.setTimeout(180_000)
    await page.getByRole('button', { name: '系统工具' }).click()
    const panel = page.getByRole('region', { name: 'HTTP 通知' })
    await expect(panel).toBeVisible()
    await panel.getByRole('button', { name: '新增通知' }).click()
    await panel.getByLabel('名称').fill(fixture.config_name)
    await panel.getByLabel('HTTP 方法').selectOption('POST')
    await panel.getByLabel('请求地址').fill('http://127.0.0.1:19091/hook')
    await panel.getByLabel('请求体模板').fill(
      '{"type":{{event.type}},"event_id":{{event.id}},"alarm":{{alarm.name}},"value":{{entity.value}}}',
    )
    await panel.getByRole('button', { name: '保存', exact: true }).click()
    await expect(panel.getByText('已保存。请求内容变化后，需要重新发送测试。')).toBeVisible()

    const card = panel.locator('article').filter({ hasText: fixture.config_name })
    await card.getByRole('button', { name: '发送测试' }).click()
    await expect(panel.getByText('测试请求已送达，可以启用。')).toBeVisible()
    await card.getByRole('button', { name: '启用', exact: true }).click()
    await expect(panel.getByText('通知已启用。')).toBeVisible()
    await expect(card).toContainText('已启用')

    const configs = await api<Record<string, unknown>[]>(
      page.request,
      apiSession,
      'GET',
      '/api/v1/admin/alarm-http-notifications',
    )
    const config = configs.find((item) => item.name === fixture.config_name)
    expect(config).toBeTruthy()
    notificationConfigId = String(config?.id)

    await expect.poll(async () => (
      (await alarmHttpReceiverStatus(environment)).records.some(
        (record) => record.body && typeof record.body === 'object' && record.body.type === 'TEST',
      )
    )).toBe(true)
    await runAlarmHttpFixture('clear-receiver', environment)
  })

  test('L2 告警发生和恢复各送达一次，确认动作不发送', async () => {
    test.setTimeout(240_000)
    expect(notificationConfigId).not.toBe('')
    await applyAlarmRule(page.request, apiSession)

    await page.getByRole('button', { name: '告警中心' }).click()
    await page.getByRole('button', { name: '告警规则', exact: true }).click()
    const configuredGroup = page.locator('section').filter({ hasText: '已配置规则' })
      .getByText(fixture.alarm_name, { exact: true })
    await expect(configuredGroup).toBeVisible()

    await publishUntilDelivered(1, 'ALARM_ACTIVATED')
    await expect.poll(async () => receiverTypes()).toContain('ALARM_ACTIVATED')

    const activeEvents = await api<{ items: Array<{ id: string; alarm_name: string }> }>(
      page.request,
      apiSession,
      'GET',
      `/api/v1/alarm-events?state=open&entity_instance_id=${encodeURIComponent(fixture.entity_id)}`,
    )
    const active = activeEvents.items.find((item) => item.alarm_name === fixture.alarm_name)
    expect(active).toBeTruthy()
    const beforeAck = (await alarmHttpReceiverStatus(environment)).records.length
    await api(
      page.request,
      apiSession,
      'POST',
      `/api/v1/alarm-events/${active?.id}/acknowledgements`,
      {},
    )
    await page.waitForTimeout(1_200)
    expect((await alarmHttpReceiverStatus(environment)).records).toHaveLength(beforeAck)

    await publishUntilDelivered(0, 'ALARM_RECOVERED')
    await expect.poll(async () => receiverTypes()).toContain('ALARM_RECOVERED')

    const records = (await alarmHttpReceiverStatus(environment)).records
      .filter((record) => (
        record.body && typeof record.body === 'object'
        && ['ALARM_ACTIVATED', 'ALARM_RECOVERED'].includes(String(record.body.type))
      ))
    expect(records).toHaveLength(2)
    expect(new Set(records.map((record) => record.idempotency_key)).size).toBe(2)
  })

  test('界面通知记录可看见发生和恢复结果', async () => {
    await page.getByRole('button', { name: '告警中心' }).click()
    await page.getByRole('button', { name: '通知记录', exact: true }).click()
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    const matching = page.locator('article').filter({ hasText: fixture.alarm_name })
    await expect(matching).toHaveCount(2)
    await expect(matching.filter({ hasText: '告警发生' })).toContainText('已送达')
    await expect(matching.filter({ hasText: '告警恢复' })).toContainText('已送达')
  })

  async function loginApi(request: APIRequestContext): Promise<ApiSession> {
    const response = await request.post('/api/v1/auth/login', {
      data: { username: environment.username, password: environment.password },
    })
    expect(response.ok()).toBeTruthy()
    const body = await response.json() as { access_token: string }
    return { token: body.access_token }
  }

  async function loginPage(target: Page) {
    await target.goto('/')
    const loginButton = target.getByRole('button', { name: '登录', exact: true })
    const navigation = target.getByRole('button', { name: '节点管理' })
    await Promise.race([
      loginButton.waitFor({ state: 'visible' }),
      navigation.waitFor({ state: 'visible' }),
    ])
    if (await loginButton.isVisible().catch(() => false)) {
      await target.getByLabel('用户名').fill(environment.username)
      await target.getByLabel('密码').fill(environment.password)
      await loginButton.click()
    }
    await expect(navigation).toBeVisible()
  }

  async function applyAlarmRule(request: APIRequestContext, session: ApiSession) {
    const ruleSet = await api<{ rule_set_id: string; revision: number }>(
      request,
      session,
      'POST',
      '/api/v1/alarm-rule-sets',
      {
        key: fixture.rule_set_key,
        name: fixture.alarm_name,
        rules: [{
          id: `fault-${environment.runId}`,
          name: fixture.alarm_name,
          severity: 'WARNING',
          trigger: { operator: 'eq', value: true },
          trigger_duration_seconds: 0,
          recovery: { operator: 'eq', value: false },
          recovery_duration_seconds: 0,
          notification_throttle_seconds: 0,
          unit: null,
          fault_map_id: null,
          http_notification_config_id: notificationConfigId,
        }],
      },
    )
    const plan = await api<{ id: string; digest: string; status: string; blockers: unknown[] }>(
      request,
      session,
      'POST',
      '/api/v1/alarm-configuration-plans',
      {
        selection: {
          entity_instance_ids: [fixture.entity_id],
          node_ids: [],
          entity_definition_ids: [],
        },
        rule_set_id: ruleSet.rule_set_id,
        rule_set_revision: ruleSet.revision,
      },
    )
    expect(plan.status, JSON.stringify(plan.blockers)).toBe('ready')
    await api(
      request,
      session,
      'POST',
      `/api/v1/alarm-configuration-plans/${plan.id}/apply`,
      { plan_digest: plan.digest },
      { 'Idempotency-Key': `alarm-http-apply-${environment.runId}` },
    )
  }

  async function deliveries(): Promise<Delivery[]> {
    const result = await api<{ items: Delivery[] }>(
      page.request,
      apiSession,
      'GET',
      '/api/v1/alarms/notification-deliveries?page=1&page_size=100',
    )
    return result.items.filter((item) => item.alarm_name === fixture.alarm_name)
  }

  async function deliveryTypes(): Promise<string[]> {
    return (await deliveries())
      .filter((item) => item.status === 'delivered')
      .map((item) => item.event_type || '')
  }

  async function publishUntilDelivered(value: number, eventType: string) {
    await expect.poll(async () => {
      await publishRawPoint(fixture.tag_key, value, environment)
      return deliveryTypes()
    }, {
      timeout: 60_000,
      intervals: [1_000],
    }).toContain(eventType)
  }

  async function receiverTypes(): Promise<string[]> {
    return (await alarmHttpReceiverStatus(environment)).records.map((record) => (
      record.body && typeof record.body === 'object' ? String(record.body.type || '') : ''
    ))
  }
})

async function api<T>(
  request: APIRequestContext,
  session: ApiSession,
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  path: string,
  data?: unknown,
  headers: Record<string, string> = {},
): Promise<T> {
  const response = await request.fetch(path, {
    method,
    data,
    headers: { Authorization: `Bearer ${session.token}`, ...headers },
  })
  if (!response.ok()) {
    throw new Error(`${method} ${path} failed: ${response.status()} ${await response.text()}`)
  }
  return response.status() === 204 ? ({} as T) : response.json() as Promise<T>
}
