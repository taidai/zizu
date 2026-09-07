import { expect, test, type Page, type Route } from '@playwright/test'

const descriptors = [
  {
    id: 'entity-a', node_id: 'node-a', node_type: 'PCS', node_display_name: '1# PCS',
    definition_id: 'pcs.active_power', display_name: '有功功率', data_type: 'float', unit: 'kW',
    direction: 'R', freshness_seconds: 60, confirmed: true,
  },
  {
    id: 'entity-b', node_id: 'node-b', node_type: 'PCS', node_display_name: '2# PCS',
    definition_id: 'pcs.active_power', display_name: '有功功率', data_type: 'float', unit: 'W',
    direction: 'R', freshness_seconds: 60, confirmed: true,
  },
]

function snapshot(nodeId: string) {
  const descriptor = descriptors.find((item) => item.node_id === nodeId)!
  const stale = nodeId === 'node-b'
  return {
    type: 'frame_snapshot', node_id: nodeId, cursor: `cursor-${nodeId}`, frame_sequence: 10,
    frame_time: '2026-09-07T01:10:00.000Z', configuration_revision: 12,
    frame_status: 'COMPLETE', failure: null, backlog_frames: 0, l0: [],
    l2: [{
      entity_instance_id: descriptor.id, node_id: nodeId, definition_id: descriptor.definition_id,
      display_name: descriptor.display_name, data_type: descriptor.data_type,
      value: stale ? 9 : 0, unit: descriptor.unit, quality: stale ? 64 : 192,
      reason: stale ? 'ENTITY_DATA_STALE' : null,
      observed_at: stale ? '2026-09-07T01:09:00.000Z' : '2026-09-07T01:10:00.000Z',
      value_observed_at: '2026-09-07T01:08:00.000Z', received_at: '2026-09-07T01:10:00.000Z',
      calculated_at: '2026-09-07T01:10:00.000Z', processing_revision_id: 'pr-1',
      configuration_revision: 12, source_digest: 'sha256:evidence', frame_sequence: 10,
    }],
  }
}

async function fulfillJson(route: Route, body: unknown, delay = 0) {
  if (delay) await new Promise((resolve) => setTimeout(resolve, delay))
  await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installFixture(page: Page) {
  const controlWrites: string[] = []
  await page.addInitScript(() => {
    class FixtureWebSocket {
      static OPEN = 1
      readyState = 1
      onopen: ((event: Event) => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      onerror: ((event: Event) => void) | null = null
      onclose: ((event: CloseEvent) => void) | null = null
      constructor() { setTimeout(() => this.onopen?.(new Event('open')), 0) }
      send(value: string) {
        const payload = JSON.parse(value)
        if (payload.authenticate) setTimeout(() => this.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type: 'authenticated' }) })), 0)
        if (payload.subscribe) setTimeout(() => this.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type: 'subscribed' }) })), 0)
      }
      close() { this.readyState = 3 }
    }
    Object.defineProperty(window, 'WebSocket', { value: FixtureWebSocket })
  })
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() !== 'GET' && /control-confirmations|control-commands|reconcile/.test(url.pathname)) {
      controlWrites.push(`${request.method()} ${url.pathname}`)
    }
    if (url.pathname.endsWith('/auth/me')) return fulfillJson(route, { user: { id: 'operator-1', username: 'tablet-runtime', role: 'operator' } })
    if (url.pathname.endsWith('/health')) return fulfillJson(route, { status: 'ok', version: 'test', uptime_seconds: 1, components: { timescaledb: { status: 'connected' }, mqtt: { status: 'connected' }, neuron: { status: 'connected' } }, pipeline: { status: 'running', messages_received: 0, points_written_db: 0, last_message_at: null } })
    if (url.pathname.endsWith('/entity-instances')) return fulfillJson(route, { items: descriptors, total: descriptors.length })
    if (url.pathname.endsWith('/alarms/counts')) return fulfillJson(route, { counts: { 'node-a': 2 } })
    if (url.pathname.endsWith('/runtime/frame-snapshot')) {
      const nodeId = url.searchParams.get('node_id')!
      return fulfillJson(route, snapshot(nodeId), nodeId === 'node-a' ? 180 : 20)
    }
    if (url.pathname.endsWith('/auth/ws-ticket')) return fulfillJson(route, { ticket: 'fixture-ticket' })
    if (url.pathname.endsWith('/ems-workbench')) return fulfillJson(route, {
      workbench_id: 'reference-delivery', configuration_revision: 12,
      navigation: [{ id: 'overview', label: '概览' }, { id: 'trends', label: '历史' }, { id: 'alarms', label: '告警' }, { id: 'controls', label: '控制' }],
      groups: [], kpis: [], trends: [], alarms: { visible: true }, controls: { visible: false, entities: [] },
    })
    return fulfillJson(route, { items: [], total: 0 })
  })
  return controlWrites
}

test('swapped node snapshots stay identity-safe, preserve zero, and expose stale evidence without navigation writes', async ({ page }) => {
  test.setTimeout(120_000)
  const controlWrites = await installFixture(page)
  await page.goto('/', { waitUntil: 'commit' })

  const nodeA = page.getByRole('article', { name: '1# PCS 运行数据' })
  const nodeB = page.getByRole('article', { name: '2# PCS 运行数据' })
  await expect(nodeA).toContainText('0')
  await expect(nodeA).toContainText('kW')
  await expect(nodeA).toContainText('当前值')
  await expect(nodeB).toContainText('9')
  await expect(nodeB).toContainText('W')
  await expect(nodeB).toContainText('最后值')
  await expect(nodeB).not.toContainText('当前正常')

  await page.getByRole('button', { name: '历史' }).click()
  await page.getByRole('button', { name: '概览' }).click()
  expect(controlWrites).toEqual([])
})
