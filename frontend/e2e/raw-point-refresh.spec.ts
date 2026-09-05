import { expect, test } from '@playwright/test'

test('实时快照故障持续重试时，原始点位刷新仍可再次使用', async ({ page }) => {
  let tagReads = 0
  let snapshotReads = 0
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname.replace('/api/v1', '')
    const reply = (json: unknown, status = 200) => route.fulfill({ status, json })
    if (path === '/auth/me') return reply({ user: { id: 'engineer', username: 'engineer', role: 'engineer' } })
    if (path === '/health') return reply({ version: 'test', pipeline: { status: 'running', messages_received: 1, points_written_db: 1, last_message_at: null }, components: { mqtt: { status: 'connected' }, neuron: { status: 'connected' } } })
    if (path === '/ems-workbench') return reply({ workbench_id: 'default', configuration_revision: 1, navigation: [], groups: [], kpis: [], trends: [], alarms: { visible: true }, controls: { visible: false, entities: [] } })
    if (path === '/nodes') return reply({ nodes: [{ id: 'node', name: '刷新测试设备', node_type: 'PCS', node_kind: 'DEVICE', parent_id: null, sort_order: 0, tag_count: 0, enabled: true }] })
    if (path === '/tags') {
      tagReads += 1
      return reply({ tags: [], total: 0, total_pages: 1 })
    }
    if (path === '/runtime/frame-snapshot') {
      snapshotReads += 1
      return reply({ detail: { message: 'snapshot unavailable' } }, 503)
    }
    if (path === '/categories') return reply({ categories: [] })
    if (path.includes('alarm')) return reply({ counts: {} })
    return reply({ detail: { code: 'UNMOCKED', message: path } }, 500)
  })
  await page.goto('/')
  await page.getByRole('button', { name: '节点管理', exact: true }).click()
  const refresh = page.getByRole('button', { name: '刷新原始点位', exact: true })
  await expect(refresh).toBeEnabled()
  await expect.poll(() => tagReads).toBeGreaterThan(0)
  const before = tagReads
  await refresh.click()
  await expect.poll(() => tagReads).toBeGreaterThan(before)
  await expect.poll(() => snapshotReads).toBeGreaterThan(1)
  await expect(refresh).toBeEnabled({ timeout: 3000 })
  const after = tagReads
  await refresh.click()
  await expect.poll(() => tagReads).toBeGreaterThan(after)
  await expect(refresh).toBeEnabled({ timeout: 3000 })
  expect(errors).toEqual([])
})
