import { expect, test, type Page } from '@playwright/test'
import type { PointProcessingPlan, Tag } from '../src/api/client'
import { openEngineeringPage } from './support/tabletNavigation'

test.use({ actionTimeout: 10_000 })
test.setTimeout(45_000)

const retryStorageKey = 'zizu.dataTrunk.applyRetry.v1'
const nodeId = 'recovery-node'
const user = { id: 'recovery-engineer', username: 'recovery-test', role: 'engineer' }
const digest = 'a'.repeat(64)
const retry = { actorId: user.id, nodeId, planId: 'original-plan', planDigest: digest, idempotencyKey: 'original-request-key' }
const points: Tag[] = ['Power', 'Current'].map((name, index) => ({
  id: `raw-${index}`, node_id: nodeId, node_name: '恢复测试节点', name, display_name: name,
  wire_data_type: 'FLOAT', data_type: 'FLOAT', tag_type: 'PHYSICAL', unit: null,
  scale_factor: 1, value_offset: 0, source_path: `fixture/group/${name}`, read_write: 'R',
  enabled: true, description: null, raw_value: 1.5, eng_value: 1.5,
  latest_ts: '2026-09-07T01:00:00Z', quality: 192, aggregate_fn: null,
  formula: null, formula_type: null, sources: null, fault_map_name: null,
}))

function makePlan(action: 'add' | 'update' | 'delete_candidate' = 'add'): PointProcessingPlan {
  return {
    id: retry.planId, node_id: nodeId, template_revision_id: 'inline-revision',
    base_configuration_revision: 1, status: 'ready', blockers: [], digest,
    items: [{ item_key: 'output', kind: 'output_binding', layer: 'L2', action, output_id: 'power', entity_definition_id: 'pcs.power' }],
  }
}

async function installFixture(page: Page, options: {
  stored?: boolean
  action?: 'add' | 'update' | 'delete_candidate'
  restore?: 'ready' | 'pending' | 'unavailable'
} = {}) {
  const requests = { drafts: [] as unknown[], applies: [] as Array<{ key: string | undefined; body: unknown; path: string }> }
  const state = { restore: options.restore || 'ready' }
  let releaseRestore: () => void = () => undefined
  const restoreGate = new Promise<void>((resolve) => { releaseRestore = resolve })
  await page.addInitScript(({ user, retry, stored, retryStorageKey }) => {
    sessionStorage.setItem('zizu.auth.session.v1', JSON.stringify({
      accessToken: 'isolated-recovery-session', expiresAt: '2099-01-01T00:00:00Z', user,
    }))
    if (stored && !sessionStorage.getItem(retryStorageKey)) sessionStorage.setItem(retryStorageKey, JSON.stringify(retry))
  }, { user, retry, stored: options.stored, retryStorageKey })
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v1/auth/me') return route.fulfill({ json: { user } })
    if (path === '/api/v1/health') return route.fulfill({ json: {
      status: 'healthy', version: 'test', uptime_seconds: 1,
      components: { timescaledb: { status: 'connected' }, mqtt: { status: 'connected' }, neuron: { status: 'connected' } },
      pipeline: { status: 'running', messages_received: 2, points_written_db: 2, last_message_at: null },
    } })
    if (path === '/api/v1/ems-workbench') return route.fulfill({ json: {
      workbench_id: 'recovery-fixture', configuration_revision: 1,
      navigation: [], groups: [], kpis: [], trends: [], alarms: { visible: false }, controls: { visible: false, entities: [] },
    } })
    if (path === '/api/v1/nodes') return route.fulfill({ json: { nodes: [{
      id: nodeId, name: '恢复测试节点', parent_id: null, layer: 1, node_type: 'PCS', sort_order: 0, enabled: true, tag_count: 2,
    }] } })
    if (path === '/api/v1/tags') return route.fulfill({ json: { tags: points, total: 2, page: 1, page_size: 10, total_pages: 1 } })
    if (path === '/api/v1/categories') return route.fulfill({ json: { categories: [] } })
    if (path === '/api/v1/alarms/counts') return route.fulfill({ json: { counts: {} } })
    if (path === '/api/v1/entity-instances' || path === '/api/v1/point-processing-templates') return route.fulfill({ json: { items: [], total: 0 } })
    if (path.endsWith('/data-trunk')) return route.fulfill({ json: {
      node_id: nodeId, l0: [], l1_summary: { installed: false, revision_id: null, output_count: 0, source_summary: [] }, l2: [],
    } })
    if (path.endsWith('/point-processing-drafts/plan')) {
      requests.drafts.push(request.postDataJSON())
      return route.fulfill({ json: makePlan() })
    }
    if (path === `/api/v1/point-processing-plans/${retry.planId}`) {
      if (state.restore === 'pending') await restoreGate
      return state.restore === 'unavailable'
        ? route.fulfill({ status: 503, json: { detail: 'Recovery temporarily unavailable' } })
        : route.fulfill({ json: makePlan(options.action) })
    }
    if (path === `/api/v1/point-processing-plans/${retry.planId}/apply`) {
      requests.applies.push({ key: request.headers()['idempotency-key'], body: request.postDataJSON(), path })
      return requests.applies.length === 1
        ? route.fulfill({ status: 503, json: { detail: 'Result unknown' } })
        : route.fulfill({ json: { id: 'application', plan_id: retry.planId, installed_processing_id: 'processing', revision_id: 'revision', configuration_revision: 2, output_entity_instance_ids: ['entity'] } })
    }
    // Fail closed: this browser contract test never forwards a request to a backend or device.
    return route.fulfill({ status: 503, json: { detail: `Unconfigured isolated boundary: ${path}` } })
  })
  return { requests, state, releaseRestore }
}

async function openRawPoints(page: Page) {
  await page.goto('/')
  await openEngineeringPage(page, '节点与数据')
  // The sole node is selected on entry; its pending plan may already open a modal.
  await expect(page.getByTitle('恢复测试节点', { exact: true })).toBeVisible()
  await expect(page.getByLabel('选择 Power', { exact: true })).toBeVisible()
}

async function storedRetry(page: Page) {
  return page.evaluate((key) => JSON.parse(sessionStorage.getItem(key) || 'null'), retryStorageKey)
}

for (const action of ['update', 'delete_candidate'] as const) {
  test(`${action} recovery belongs to the existing lifecycle entry and cannot be replaced by a new inline plan`, async ({ page }) => {
    const { requests } = await installFixture(page, { stored: true, action })
    await openRawPoints(page)
    await expect(page.getByRole('alert')).toBeVisible({ timeout: 5000 })
    expect(await storedRetry(page)).toEqual(retry)
    await expect(page.getByRole('alert')).toContainText('标准实体')
    await page.getByLabel('选择 Power', { exact: true }).check()
    await expect(page.getByRole('button', { name: '加工为实体', exact: true })).toBeDisabled()
    expect(requests.drafts).toEqual([])
    expect(requests.applies).toEqual([])
  })
}

test('switching to the lifecycle tab during a failed recovery retains the original key and offers a safe reread', async ({ page }) => {
  const { requests, state } = await installFixture(page, { stored: true })
  await openRawPoints(page)
  await page.getByRole('dialog', { name: '新建标准实体' }).getByRole('button', { name: '取消', exact: true }).click()
  state.restore = 'unavailable'
  await page.getByRole('button', { name: '标准实体', exact: true }).click()
  await expect(page.getByText('标准实体不可用', { exact: true })).toBeVisible({ timeout: 5000 })
  expect(await storedRetry(page)).toEqual(retry)
  await expect(page.getByRole('button', { name: '重新读取', exact: true })).toBeVisible()
  expect(requests.drafts).toEqual([])
  expect(requests.applies).toEqual([])
  state.restore = 'ready'
  await page.getByRole('button', { name: '重新读取', exact: true }).click()
  await expect(page.getByRole('heading', { name: '标准实体', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '原始数据', exact: true }).click()
  const editor = page.getByRole('dialog', { name: '新建标准实体' })
  await expect(editor).toBeVisible()
  await expect(editor.getByLabel('实体名称', { exact: true })).toBeDisabled()
  expect(await storedRetry(page)).toEqual(retry)
})

test('unknown inline apply freezes all inputs and preserves its exact request through close, point change and tab change', async ({ page }) => {
  const { requests } = await installFixture(page)
  await openRawPoints(page)
  await page.getByLabel('选择 Power', { exact: true }).check()
  await page.getByRole('button', { name: '加工为实体', exact: true }).click()
  const editor = page.getByRole('dialog', { name: '新建标准实体' })
  await editor.getByLabel('实体名称', { exact: true }).fill('Test power')
  await editor.getByRole('button', { name: '检查结果', exact: true }).click()
  await editor.getByRole('button', { name: '发布实体', exact: true }).click()
  await expect(editor.getByRole('alert')).toContainText('结果未知')
  await expect(editor.getByLabel('实体名称', { exact: true })).toBeDisabled({ timeout: 5000 })
  for (const input of await editor.locator('input, select, textarea').all()) await expect(input).toBeDisabled()
  await expect(editor.getByRole('button', { name: '检查结果', exact: true })).toBeDisabled()
  const original = await storedRetry(page)
  await editor.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: '继续上次发布', exact: true }).click()
  await expect(editor.getByLabel('实体名称', { exact: true })).toBeDisabled()
  expect(await storedRetry(page)).toEqual(original)
  await editor.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByLabel('选择 Current', { exact: true }).check()
  await expect(editor).toBeVisible()
  await expect(editor.getByLabel('实体名称', { exact: true })).toBeDisabled()
  expect(await storedRetry(page)).toEqual(original)
  await editor.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: '标准实体', exact: true }).click()
  await expect(page.getByRole('heading', { name: '标准实体', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '原始数据', exact: true }).click()
  await expect(editor).toBeVisible()
  await expect(editor.getByLabel('实体名称', { exact: true })).toBeDisabled()
  expect(await storedRetry(page)).toEqual(original)
  await editor.getByRole('button', { name: '继续上次发布', exact: true }).click()
  await expect(editor).toContainText('标准实体已发布')
  expect(requests.drafts).toHaveLength(1)
  expect(requests.applies).toHaveLength(2)
  expect(requests.applies[1]).toEqual(requests.applies[0])
  expect(await storedRetry(page)).toBeNull()
})

for (const restore of ['pending', 'unavailable'] as const) {
  test(`${restore} recovery blocks new inline drafts until the original request is restored`, async ({ page }) => {
    const { requests, state, releaseRestore } = await installFixture(page, { stored: true, restore })
    await openRawPoints(page)
    await page.getByLabel('选择 Power', { exact: true }).check()
    if (restore === 'unavailable') await expect(page.getByRole('button', { name: '重试恢复', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '加工为实体', exact: true })).toBeDisabled({ timeout: 5000 })
    expect(await storedRetry(page)).toEqual(retry)
    expect(requests.drafts).toEqual([])
    state.restore = 'ready'
    if (restore === 'pending') releaseRestore()
    else await page.getByRole('button', { name: '重试恢复', exact: true }).click()
    const editor = page.getByRole('dialog', { name: '新建标准实体' })
    await expect(editor).toBeVisible()
    await expect(editor.getByLabel('实体名称', { exact: true })).toBeDisabled()
    expect(await storedRetry(page)).toEqual(retry)
  })
}
