import { expect, test, type Page } from '@playwright/test'

test.use({ actionTimeout: 10_000 })
test.setTimeout(30_000)

const pageErrors = new WeakMap<Page, string[]>()
test.beforeEach(({ page }) => {
  const errors: string[] = []
  pageErrors.set(page, errors)
  page.on('pageerror', (error) => errors.push(error.message))
})
test.afterEach(({ page }) => {
  expect(pageErrors.get(page)).toEqual([])
})

async function installSession(page: Page, role: 'admin' | 'engineer' | 'operator', healthFails = false) {
  const user = { id: `tablet-${role}`, username: `test-${role}`, role }
  const writes: string[] = []
  await page.addInitScript((testUser) => {
    sessionStorage.setItem('zizu.auth.session.v1', JSON.stringify({
      accessToken: 'isolated-tablet-test-session', expiresAt: '2099-01-01T00:00:00Z', user: testUser,
    }))
  }, user)
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (!path.startsWith('/api/')) return route.fallback()
    if (request.method() !== 'GET') writes.push(`${request.method()} ${path}`)
    if (path === '/api/v1/auth/me') return route.fulfill({ json: { user } })
    if (path === '/api/v1/auth/logout') return route.fulfill({ json: { status: 'ok' } })
    if (path === '/api/v1/health') {
      return healthFails
        ? route.fulfill({ status: 503, json: { detail: 'isolated test connection unavailable' } })
        : route.fulfill({ json: {
          status: 'healthy', version: 'test', uptime_seconds: 120,
          components: { timescaledb: { status: 'connected' }, mqtt: { status: 'connected' }, neuron: { status: 'connected' } },
          pipeline: { status: 'running', messages_received: 12, points_written_db: 12, last_message_at: new Date().toISOString() },
        } })
    }
    if (path === '/api/v1/ems-workbench') return route.fulfill({ json: {
      workbench_id: 'isolated-empty-site', configuration_revision: 1,
      navigation: [{ id: 'overview', label: '运行总览' }, { id: 'controls', label: '授权控制' }],
      groups: [], kpis: [], trends: [], alarms: { visible: true }, controls: { visible: false, entities: [] },
    } })
    if (path === '/api/v1/entity-instances') return route.fulfill({ json: { items: [], total: 0 } })
    if (path === '/api/v1/nodes') return route.fulfill({ json: { nodes: [] } })
    if (path === '/api/v1/categories') return route.fulfill({ json: { categories: [] } })
    if (path === '/api/v1/alarms/counts') return route.fulfill({ json: { counts: {} } })
    return route.fulfill({ status: 404, json: { detail: `Unconfigured isolated boundary: ${path}` } })
  })
  return writes
}

async function expectTouchTargets(page: Page, locator: ReturnType<Page['locator']>) {
  for (const target of await locator.all()) {
    const box = await target.boundingBox()
    expect(box?.height).toBeGreaterThanOrEqual(44)
  }
}

test('operator has bounded runtime navigation without engineering authority', async ({ page }, testInfo) => {
  const writes = await installSession(page, 'operator')
  await page.setViewportSize({ width: 1280, height: 800 })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '自足IOT', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '工程配置', exact: true })).toHaveCount(0)
  const runtimeNavigation = page.getByRole('navigation', { name: '日常运行' })
  await expect(runtimeNavigation.getByRole('button')).toHaveCount(3)
  await expect(runtimeNavigation.getByRole('button').allTextContents()).resolves.toEqual(['总览', '设备监控', '手动控制'])
  const nav = await runtimeNavigation.boundingBox()
  expect(nav && nav.y + nav.height).toBeLessThanOrEqual(800)
  await expectTouchTargets(page, runtimeNavigation.getByRole('button'))
  await expectTouchTargets(page, page.getByRole('banner').getByRole('button'))
  await runtimeNavigation.getByRole('button', { name: '设备监控' }).click()
  await expect(page.getByRole('heading', { name: '设备监控', exact: true })).toBeVisible()
  expect(writes).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('runtime-1280.png') })
})

test('engineer opens existing node configuration without admin tools or implicit writes', async ({ page }, testInfo) => {
  const writes = await installSession(page, 'engineer')
  await page.setViewportSize({ width: 1024, height: 768 })
  await page.goto('/')
  await page.getByRole('banner').getByRole('button', { name: '工程配置', exact: true }).click()
  const engineering = page.getByRole('navigation', { name: '工程配置导航' })
  await expect(engineering.getByRole('button').allTextContents()).resolves.toEqual(['节点与数据', '告警', '调度策略'])
  await expect(engineering.getByRole('button', { name: '节点与数据', exact: true })).toBeVisible()
  await expect(engineering.getByRole('button', { name: '调度策略', exact: true })).toBeVisible()
  await expect(engineering.getByRole('button', { name: '系统工具', exact: true })).toHaveCount(0)
  await expectTouchTargets(page, engineering.getByRole('button'))
  await expectTouchTargets(page, page.getByRole('banner').getByRole('button'))
  await page.screenshot({ path: testInfo.outputPath('engineering-1024.png') })
  await page.getByRole('button', { name: '返回现场', exact: true }).click()
  await expect(page.getByRole('navigation', { name: '日常运行' })).toBeVisible()
  expect(writes).toEqual([])
})

test('admin tools remain reachable and unavailable health is not green', async ({ page }) => {
  await installSession(page, 'admin', true)
  await page.goto('/')
  await expect(page.getByRole('status', { name: '数据链路状态' })).toContainText('连接未知')
  await page.getByRole('banner').getByRole('button', { name: '工程配置', exact: true }).click()
  const engineering = page.getByRole('navigation', { name: '工程配置导航' })
  await expect(engineering.getByRole('button').allTextContents()).resolves.toEqual(['节点与数据', '告警', '调度策略', '系统工具'])
  await expect(engineering.getByRole('button', { name: '系统工具', exact: true })).toBeVisible()
})

test('overview keeps alarm events reachable without adding alarm to runtime navigation', async ({ page }) => {
  await installSession(page, 'operator')
  await page.goto('/')
  await expect(page.getByRole('navigation', { name: '日常运行' }).getByRole('button', { name: '告警', exact: true })).toHaveCount(0)
  await page.getByRole('region', { name: '待处理告警' }).getByRole('button', { name: /^查看告警/ }).click()
  await expect(page.getByRole('heading', { name: '告警中心', exact: true })).toBeVisible()
})

test('production shell uses the approved red gold bright-silver tokens and excludes demo controls', async ({ page }) => {
  await installSession(page, 'admin')
  await page.goto('/')
  const tokens = await page.evaluate(() => {
    const styles = getComputedStyle(document.documentElement)
    return {
      red: styles.getPropertyValue('--zizu-red').trim(),
      redLight: styles.getPropertyValue('--zizu-red-light').trim(),
      gold: styles.getPropertyValue('--zizu-gold').trim(),
      goldLight: styles.getPropertyValue('--zizu-gold-light').trim(),
      silver: styles.getPropertyValue('--zizu-silver').replaceAll(' ', ''),
    }
  })
  expect(tokens).toEqual({
    red: '#bb0814',
    redLight: '#e41622',
    gold: '#b88a34',
    goldLight: '#f3d88b',
    silver: 'linear-gradient(135deg,#fff0%,#f0f2f435%,#e0e4e8100%)',
  })
  await expect(page.getByText(/前端\s*DEMO|切换场景|切换角色|重置演示|模拟回读/)).toHaveCount(0)
})

for (const role of ['operator', 'engineer'] as const) {
  test(`${role} sees directory failure instead of an empty station and can retry`, async ({ page }) => {
    const writes = await installSession(page, role)
    let fails = true
    await page.route('**/api/v1/nodes', (route) => fails
      ? route.fulfill({ status: 503, json: { detail: 'isolated directory unavailable' } })
      : route.fulfill({ json: { nodes: [] } }))
    await page.goto('/')
    if (role === 'operator') {
      await page.getByRole('navigation', { name: '日常运行' }).getByRole('button', { name: '设备监控' }).click()
    } else {
      await page.getByRole('banner').getByRole('button', { name: '工程配置', exact: true }).click()
    }
    const directoryAlert = role === 'operator'
      ? page.getByRole('alert').filter({ hasText: '节点目录读取失败' })
      : page.getByRole('alert', { name: '节点目录状态' })
    await expect(directoryAlert).toContainText(role === 'operator' ? '现有结果不按空站处理' : '节点目录加载失败')
    await expect(page.getByText(/暂无运行节点|暂无节点，点击/)).toHaveCount(0)
    fails = false
    await page.getByRole('button', { name: role === 'operator' ? '重试节点' : '重试加载节点' }).click()
    await expect(directoryAlert).toHaveCount(0)
    await expect(page.getByText(role === 'operator' ? '当前没有可监控节点' : '暂无节点，点击「+ 节点」创建根节点', { exact: true })).toBeVisible()
    expect(writes).toEqual([])
  })
}

test('directory refresh failure keeps the last known tree with a stale notice', async ({ page }) => {
  await installSession(page, 'engineer')
  let fails = false
  await page.route('**/api/v1/nodes', (route) => fails
    ? route.fulfill({ status: 503, json: { detail: 'isolated directory unavailable' } })
    : route.fulfill({ json: { nodes: [{ id: 'retained-node', parent_id: null, name: '保留目录节点', node_type: 'PCS', layer: 1, sort_order: 0, tag_count: 0 }] } }))
  await page.goto('/')
  await page.getByRole('banner').getByRole('button', { name: '工程配置', exact: true }).click()
  await expect(page.getByTitle('保留目录节点', { exact: true })).toBeVisible()
  fails = true
  await page.getByRole('heading', { name: '节点管理', exact: true }).locator('..').getByRole('button', { name: '刷新', exact: true }).click()
  await expect(page.getByRole('alert', { name: '节点目录状态' })).toContainText('保留上次目录，可能不是最新')
  await expect(page.getByTitle('保留目录节点', { exact: true })).toBeVisible()
})
