import { expect, test, type Page, type Route } from '@playwright/test'

const nodes = Array.from({ length: 8 }, (_, index) => ({
  id: `device-${index + 1}`,
  name: index < 2 ? '同名 PCS' : `${index + 1}# 设备`,
  parent_id: 'site-1',
  layer: 4,
  node_type: ['PCS', 'PCS', 'INVERTER', 'BMS', 'EVSE', 'METER', 'CUSTOM_DEVICE', ''][index],
  sort_order: index,
  enabled: true,
  tag_count: 0,
}))

const entities = [
  ...Array.from({ length: 23 }, (_, index) => ({
    id: `entity-${index + 1}`,
    node_id: index < 22 ? 'device-1' : 'device-2',
    node_type: 'PCS',
    node_display_name: '同名 PCS',
    definition_id: index === 0 ? 'pcs.running_state' : index === 1 ? 'pcs.activePower' : index === 2 ? 'pcs.temp' : `pcs.metric_${index + 1}`,
    display_name: index === 0 ? '运行状态' : index === 1 ? '有功功率' : index === 2 ? '内部温度' : `指标 ${index + 1}`,
    data_type: index === 0 ? 'bool' : 'float',
    unit: index === 0 ? null : 'kW',
    direction: 'R',
    freshness_seconds: 60,
    confirmed: true,
  })),
  {
    id: 'entity-unknown',
    node_id: 'device-2',
    node_type: 'PCS',
    node_display_name: '同名 PCS',
    definition_id: 'pcs.unknown_signal',
    display_name: '未知量',
    data_type: 'float',
    unit: 'kW',
    direction: 'R',
    freshness_seconds: 60,
    confirmed: true,
  },
]

function frame(nodeId: string) {
  const selected = entities.filter((entity) => entity.node_id === nodeId)
  return {
    type: 'frame_snapshot', node_id: nodeId, cursor: `cursor-${nodeId}`, frame_sequence: 7,
    frame_time: '2026-09-07T02:00:00.000Z', configuration_revision: 12,
    frame_status: 'COMPLETE', failure: null, backlog_frames: 0, l0: [{
      tag_id: `raw-${nodeId}`, node_id: nodeId, name: 'StatusWord', display_name: '原始状态字',
      data_type: 'int', value: 2, unit: null, source_quality: 192, effective_quality: 64,
      source_timestamp: '2026-09-07T01:59:58.000Z', received_at: '2026-09-07T01:59:59.000Z',
      accepted_beat: 6, source_path: 'gateway/group/StatusWord', source_type: 'neuron', frame_sequence: 7,
    }],
    l2: selected.map((entity, index) => {
      const quality = entity.id === 'entity-1' ? 64 : entity.id === 'entity-3' ? 0 : entity.id === 'entity-unknown' ? 1 : 192
      return {
        entity_instance_id: entity.id, node_id: nodeId, definition_id: entity.definition_id,
        display_name: entity.display_name, data_type: entity.data_type,
        value: entity.id === 'entity-unknown' ? null : entity.data_type === 'bool' ? false : index + 1, unit: entity.unit, quality,
        reason: quality === 64 ? 'ENTITY_DATA_STALE' : quality === 0 ? 'INPUT_BAD' : quality === 1 ? 'NO_DATA' : null,
        observed_at: entity.id === 'entity-3' ? '2026-09-07T01:59:55.000Z' : entity.id === 'entity-unknown' ? '2026-09-07T01:59:50.000Z' : '2026-09-07T02:00:00.000Z',
        value_observed_at: entity.id === 'entity-1' ? '2026-09-07T01:59:57.000Z' : entity.id === 'entity-3' || entity.id === 'entity-unknown' ? null : '2026-09-07T02:00:00.000Z',
        received_at: '2026-09-07T02:00:00.000Z', calculated_at: '2026-09-07T02:00:00.000Z',
        processing_revision_id: 'pr-1', configuration_revision: 12,
        source_digest: `sha256:${entity.id}`, frame_sequence: 7,
      }
    }),
  }
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installFixture(page: Page, options: {
  nodeFailures?: number
  countSequence?: Array<'success' | 'fail' | 'pending-success'>
  trunkFailures?: number
  installedRevision?: number | null
} = {}) {
  const writes: string[] = []
  let nodeCalls = 0
  let countCalls = 0
  let trunkCalls = 0
  await page.addInitScript(() => {
    const sockets: FixtureWebSocket[] = []
    class FixtureWebSocket {
      readyState = 1
      onopen: ((event: Event) => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      onerror: ((event: Event) => void) | null = null
      onclose: ((event: CloseEvent) => void) | null = null
      nodeId = ''
      constructor() {
        sockets.push(this)
        setTimeout(() => this.onopen?.(new Event('open')), 0)
      }
      send(value: string) {
        const payload = JSON.parse(value)
        if (payload.authenticate) setTimeout(() => this.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type: 'authenticated' }) })), 0)
        if (payload.subscribe) {
          this.nodeId = payload.subscribe.node_id
          setTimeout(() => this.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ type: 'subscribed' }) })), 0)
        }
      }
      close() { this.readyState = 3 }
      disconnect() { this.readyState = 3; this.onclose?.(new CloseEvent('close', { code: 1006 })) }
    }
    Object.assign(window, { __disconnectDeviceNode: (nodeId: string) => sockets.findLast((socket) => socket.nodeId === nodeId)?.disconnect() })
    Object.defineProperty(window, 'WebSocket', { value: FixtureWebSocket })
  })
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() !== 'GET' && !url.pathname.endsWith('/auth/ws-ticket')) writes.push(`${request.method()} ${url.pathname}`)
    if (url.pathname.endsWith('/nodes')) {
      nodeCalls += 1
      return nodeCalls <= (options.nodeFailures || 0)
        ? fulfillJson(route, { nodes: [], error: 'node directory unavailable' })
        : fulfillJson(route, { nodes })
    }
    if (url.pathname.endsWith('/entity-instances')) return fulfillJson(route, { items: entities, total: entities.length })
    if (url.pathname.endsWith('/alarms/counts')) {
      const outcome = options.countSequence?.[countCalls++] || 'success'
      if (outcome === 'pending-success') await new Promise((resolve) => setTimeout(resolve, 1_500))
      return outcome === 'fail'
        ? fulfillJson(route, { detail: 'count unavailable' }, 503)
        : fulfillJson(route, { counts: { 'device-1': 2, 'device-2': 1 } })
    }
    if (url.pathname.endsWith('/runtime/frame-snapshot')) return fulfillJson(route, frame(url.searchParams.get('node_id')!))
    if (url.pathname.endsWith('/auth/ws-ticket')) return fulfillJson(route, { ticket: 'fixture-ticket' })
    if (url.pathname.endsWith('/data-trunk')) {
      trunkCalls += 1
      if (trunkCalls <= (options.trunkFailures || 0)) return fulfillJson(route, { detail: 'trunk unavailable' }, 503)
      return fulfillJson(route, {
        node_id: 'device-1', l0: [],
        l1_summary: { installed: true, revision_id: 'pr-1', configuration_revision: options.installedRevision === undefined ? 12 : options.installedRevision, output_count: 2, source_summary: [] },
        l2: [
          { entity_instance_id: 'entity-1', output_key: 'state', processing_kind: 'boolean_map', source_summary: [{ input_id: 'raw-state', source_kind: 'l0', source_key: 'StatusWord' }] },
          { entity_instance_id: 'entity-2', output_key: 'power', processing_kind: 'passthrough', source_summary: [{ input_id: 'power', source_kind: 'l2', source_key: 'pcs.active_power' }] },
        ],
      })
    }
    const historyMatch = url.pathname.match(/\/entity-instances\/([^/]+)\/history$/)
    if (historyMatch) return fulfillJson(route, { items: [{
      type: 'entity_observation', event_id: 'status-history-1', entity_instance_id: historyMatch[1],
      definition_id: 'pcs.running_state', value: false, data_type: 'bool', unit: null, quality: 192,
      reason: null, observed_at: '2026-09-07T01:59:00.000Z', age_ms: 0,
      processing_revision_id: 'pr-1', configuration_revision: 12, source_digest: 'sha256:status',
    }] })
    const realtimeMatch = url.pathname.match(/\/entity-instances\/([^/]+)\/realtime$/)
    if (realtimeMatch) return fulfillJson(route, {
      type: 'entity_observation', event_id: 'status-current', entity_instance_id: realtimeMatch[1],
      definition_id: 'pcs.running_state', value: false, data_type: 'bool', unit: null, quality: 192,
      reason: null, observed_at: '2026-09-07T02:00:00.000Z', age_ms: 0,
      processing_revision_id: 'pr-1', configuration_revision: 12, source_digest: 'sha256:status',
    })
    return fulfillJson(route, {})
  })
  return writes
}

async function mountDeviceMonitor(page: Page, options: { engineering?: boolean } = {}) {
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await page.evaluate(async ({ engineering }) => {
    document.head.innerHTML = '<meta charset="UTF-8"><title>Device monitor fixture</title>'
    document.body.innerHTML = '<div id="root"></div>'
    const RefreshRuntime = (await import('/@react-refresh')).default
    RefreshRuntime.injectIntoGlobalHook(window)
    Object.assign(window, {
      $RefreshReg$: () => {},
      $RefreshSig$: () => (type: unknown) => type,
      __vite_plugin_react_preamble_installed__: true,
    })
    const React = (await import('/@id/react')).default
    const ReactDOM = (await import('/@id/react-dom/client')).default
    const DeviceMonitorPage = (await import('/src/pages/DeviceMonitorPage.tsx')).default
    const props = engineering ? { onOpenEngineering: (nodeId: string) => Object.assign(window, { __engineeringNodeId: nodeId }) } : {}
    ReactDOM.createRoot(document.getElementById('root')!).render(React.createElement(DeviceMonitorPage, props))
  }, options)
}

test('six-card pages keep ids isolated, show unconfigured nodes, and paginate 10 or 20 entity rows', async ({ page }) => {
  test.setTimeout(180_000)
  const writes = await installFixture(page)
  await mountDeviceMonitor(page)

  const summary = page.getByRole('region', { name: '设备监控摘要' })
  await expect(summary).toContainText('总设备8')
  await expect(summary).toContainText('有活动告警2')
  await expect(page.getByRole('group', { name: '设备分类' }).getByRole('button', { name: '储能', exact: true })).toBeVisible()
  await expect(page.getByRole('article')).toHaveCount(6)
  const sameName = page.getByRole('article', { name: /同名 PCS 设备卡片/ })
  await expect(sameName).toHaveCount(2)
  await expect(sameName.nth(0)).toContainText('device-1')
  await expect(sameName.nth(0)).toContainText('活动告警 2')
  await expect(sameName.nth(1)).toContainText('device-2')
  await expect(sameName.nth(1)).toContainText('活动告警 1')
  await expect(sameName.nth(0)).toContainText('超时 · 最后值（非当前）')
  await expect(sameName.nth(0)).toContainText('异常 · 最后值（非当前）')
  await expect(sameName.nth(1)).toContainText('正常 · 当前值')
  const unknownMetric = sameName.nth(1).locator('.runtime-device-card__secondary').getByRole('button', { name: '未知量' })
  await expect(unknownMetric).toContainText('—')
  await expect(unknownMetric).toContainText('未知 · 当前状态不确定')
  await expect(unknownMetric).not.toContainText('最后值')
  await expect(unknownMetric.locator('time')).toHaveCount(0)
  const primaryMetric = sameName.nth(0).locator('.runtime-device-card__primary')
  const secondaryMetrics = sameName.nth(0).locator('.runtime-device-card__secondary')
  await expect(primaryMetric).toContainText('有功功率')
  await expect(primaryMetric).toContainText('正常 · 当前值')
  await expect(secondaryMetrics).toContainText('内部温度')
  await expect(secondaryMetrics.locator('.runtime-quality')).toHaveCount(2)
  await expect(secondaryMetrics.locator('time')).toHaveCount(2)
  await expect(secondaryMetrics.locator('time').nth(0)).toHaveAttribute('datetime', '2026-09-07T01:59:55.000Z')
  await expect(secondaryMetrics.locator('time').nth(1)).toHaveAttribute('datetime', '2026-09-07T01:59:57.000Z')
  const secondaryEvidenceGeometry = await secondaryMetrics.locator('.runtime-quality, time').evaluateAll((items) => items.map((item) => {
    const rect = item.getBoundingClientRect()
    const card = item.closest('article')!.getBoundingClientRect()
    const style = getComputedStyle(item)
    return {
      width: rect.width,
      height: rect.height,
      visibility: style.visibility,
      display: style.display,
      clip: style.clip,
      position: style.position,
      opacity: Number(style.opacity),
      insideCard: rect.top >= card.top && rect.left >= card.left && rect.bottom <= card.bottom && rect.right <= card.right,
    }
  }))
  expect(secondaryEvidenceGeometry.every((item) => item.width > 20 && item.height > 8 && item.visibility === 'visible' && item.display !== 'none' && item.clip === 'auto' && item.position !== 'absolute' && item.opacity === 1 && item.insideCard)).toBe(true)

  await page.evaluate(() => (window as Window & { __disconnectDeviceNode: (nodeId: string) => void }).__disconnectDeviceNode('device-1'))
  await expect(sameName.nth(0)).toContainText('正常 · 最后值（非当前）')
  await expect(primaryMetric.locator('time')).toHaveAttribute('datetime', '2026-09-07T02:00:00.000Z')
  await expect(primaryMetric.locator('time')).toBeVisible()

  const search = page.getByRole('searchbox', { name: '名称或 ID' })
  await search.fill('DEVICE-2')
  await expect(page.getByRole('article')).toHaveCount(1)
  await expect(page.getByRole('article')).toContainText('device-2')
  await search.fill('')
  const categories = page.getByRole('group', { name: '设备分类' })
  await categories.getByRole('button', { name: '其他', exact: true }).click()
  await expect(page.getByRole('article')).toHaveCount(2)
  await expect(page.getByRole('article').first()).toContainText('device-7')
  await categories.getByRole('button', { name: '全部', exact: true }).click()
  await expect(page.getByRole('article')).toHaveCount(6)

  await search.fill('不存在的设备')
  await expect(page.getByText('没有符合条件的设备')).toBeVisible()
  await page.getByRole('button', { name: '清除筛选', exact: true }).click()
  await expect(page.getByRole('article')).toHaveCount(6)

  await page.getByRole('checkbox', { name: '仅有未恢复告警' }).check()
  await expect(page.getByRole('article')).toHaveCount(2)
  await expect(page.getByRole('article', { name: /同名 PCS 设备卡片/ })).toHaveCount(2)
  await page.getByRole('checkbox', { name: '仅有未恢复告警' }).uncheck()

  await sameName.nth(0).getByRole('button', { name: '查看详情' }).click()
  const deviceDialog = page.getByRole('dialog', { name: '同名 PCS' })
  await expect(deviceDialog).toContainText('下表显示实时实体；点击实体打开正式历史趋势与来源证据。')
  await expect(deviceDialog.locator('.runtime-device-detail__table tbody tr')).toHaveCount(10)
  await deviceDialog.getByRole('button', { name: '运行状态' }).click()
  const history = page.getByRole('region', { name: '实体历史' })
  await expect(history).toContainText('false')
  await expect(history.getByRole('img')).toHaveCount(0)
  await deviceDialog.getByRole('button', { name: '下一页' }).evaluate((button: HTMLButtonElement) => button.click())
  await expect(page.getByRole('dialog', { name: '运行状态' })).toHaveCount(0)
  await expect(deviceDialog.getByRole('button', { name: '指标 17' })).toBeVisible()
  await deviceDialog.locator('select').selectOption('20')
  await expect(deviceDialog.locator('.runtime-device-detail__table tbody tr')).toHaveCount(20)
  await deviceDialog.getByRole('button', { name: '关闭' }).click()
  await sameName.nth(0).getByRole('button', { name: '查看详情' }).click()
  await expect(deviceDialog.getByRole('combobox', { name: '实体每页条数' })).toHaveValue('10')
  await expect(deviceDialog.locator('.runtime-device-detail__table tbody tr')).toHaveCount(10)
  await deviceDialog.getByRole('button', { name: '关闭' }).click()

  await page.getByRole('button', { name: '下一页' }).click()
  await expect(page.getByRole('article')).toHaveCount(2)
  await expect(page.getByRole('article', { name: '8# 设备 设备卡片' })).toContainText('L2 未配置')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  expect(writes).toEqual([])
})

for (const viewport of [{ width: 1024, height: 768 }, { width: 1280, height: 800 }]) {
  test(`six real device cards form a two-by-three tablet grid at ${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport)
    await installFixture(page)
    await mountDeviceMonitor(page)
    await expect(page.getByRole('article')).toHaveCount(6)
    const geometry = await page.getByRole('article').evaluateAll((cards) => {
      const lefts = new Set(cards.map((card) => Math.round(card.getBoundingClientRect().left)))
      const tops = new Set(cards.map((card) => Math.round(card.getBoundingClientRect().top)))
      return { columns: lefts.size, rows: tops.size, overflow: document.documentElement.scrollWidth > innerWidth }
    })
    expect(geometry).toEqual({ columns: 3, rows: 2, overflow: false })
    await page.screenshot({ path: testInfo.outputPath(`device-monitor-${viewport.width}x${viewport.height}.png`), fullPage: true })
  })
}

test('failed alarm counts stay unknown and cannot masquerade as zero or a valid alarm filter', async ({ page }) => {
  test.setTimeout(180_000)
  await installFixture(page, { countSequence: ['fail'] })
  await mountDeviceMonitor(page)

  await expect(page.getByRole('alert')).toContainText('计数显示未知')
  await expect(page.getByText('活动告警 —').first()).toBeVisible()
  await expect(page.getByRole('checkbox', { name: '仅有未恢复告警' })).toBeDisabled()
})

test('entity provenance shows committed raw evidence, retries trunk failure and labels cross-node L2 honestly', async ({ page }, testInfo) => {
  const requests: string[] = []
  page.on('request', (request) => requests.push(request.url()))
  await installFixture(page, { trunkFailures: 1 })
  await mountDeviceMonitor(page)
  await page.getByRole('article', { name: /同名 PCS 设备卡片/ }).first().getByRole('button', { name: '查看详情' }).click()
  const device = page.getByRole('dialog', { name: '同名 PCS' })
  await device.getByRole('button', { name: '运行状态' }).click()
  const detail = page.getByRole('dialog', { name: '运行状态', exact: true })
  await expect(detail).toContainText('来源证据不可用')
  await detail.getByRole('button', { name: '重试来源' }).click()
  const provenance = detail.getByRole('region', { name: '实体来源证据' })
  await expect(provenance).toContainText('L2 运行状态')
  await expect(provenance).toContainText('L1 boolean_map · pr-1')
  await expect(provenance).toContainText('L0 原始状态字')
  await expect(provenance).toContainText('gateway/group/StatusWord')
  await expect(provenance).toContainText('原始值2')
  await expect(provenance).toContainText('源质量正常')
  await expect(provenance).toContainText('有效质量超时')
  await expect(provenance).toContainText('数据时间')
  await expect(provenance).toContainText('接收时间')
  await detail.getByRole('button', { name: '6小时', exact: true }).click()
  await expect(detail.getByRole('region', { name: '实体历史' })).toContainText('false')
  expect(requests.filter((url) => url.endsWith('/data-trunk'))).toHaveLength(2)
  expect(requests.filter((url) => /entity-instances\/[^/]+\/realtime/.test(url))).toEqual([])
  await page.screenshot({ path: testInfo.outputPath('l0-provenance.png'), fullPage: true })
  await detail.getByRole('button', { name: '关闭', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '有功功率', exact: true }).click()
  const crossSource = page.getByRole('region', { name: '实体来源证据' })
  await expect(crossSource).toContainText('跨节点 L2 pcs.active_power')
  await expect(crossSource).toContainText('需另行打开来源节点证据')
  await expect(crossSource).not.toContainText('原始状态字')
  await page.screenshot({ path: testInfo.outputPath('l2-provenance.png'), fullPage: true })
})

test('a failed node directory is retryable and never rendered as an empty site', async ({ page }) => {
  test.setTimeout(180_000)
  await installFixture(page, { nodeFailures: 1 })
  await mountDeviceMonitor(page)

  const failure = page.getByRole('alert').filter({ hasText: '节点目录读取失败' })
  await expect(failure).toBeVisible()
  await expect(page.getByText('当前没有可监控节点')).toHaveCount(0)
  await failure.getByRole('button', { name: '重试节点' }).click()
  await expect(page.getByRole('article')).toHaveCount(6)
})

test('same-template configuration rebinding cannot certify the old L2 source', async ({ page }) => {
  await installFixture(page, { installedRevision: 13 })
  await mountDeviceMonitor(page)
  await page.getByRole('article', { name: /同名 PCS 设备卡片/ }).first().getByRole('button', { name: '运行状态' }).click()
  const evidence = page.getByRole('region', { name: '实体来源证据' })
  await expect(evidence).toContainText('来源证据不可用')
  await expect(evidence).toContainText('安装配置修订')
  await expect(evidence).not.toContainText('L0 原始状态字')
  await expect(evidence).not.toContainText('gateway/group/StatusWord')
})

test('alarm retry remains disabled while counts are unknown and pending', async ({ page }) => {
  test.setTimeout(180_000)
  await installFixture(page, { countSequence: ['success', 'fail', 'pending-success'] })
  await mountDeviceMonitor(page)

  const filter = page.getByRole('checkbox', { name: '仅有未恢复告警' })
  await expect(filter).toBeEnabled()
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  const failure = page.getByRole('alert').filter({ hasText: '告警计数读取失败' })
  await expect(failure).toBeVisible()
  await failure.getByRole('button', { name: '重试计数' }).click()
  await expect(filter).toBeDisabled()
  await expect(page.getByRole('article')).toHaveCount(6)
  await expect(filter).toBeEnabled()
})

test('configuration jump stays hidden when the shell does not grant engineering access', async ({ page }) => {
  await installFixture(page)
  await mountDeviceMonitor(page)
  await page.getByRole('article', { name: /同名 PCS 设备卡片/ }).first().getByRole('button', { name: '查看详情' }).click()
  await expect(page.getByRole('dialog', { name: '同名 PCS' }).getByRole('button', { name: '配置此设备' })).toHaveCount(0)
})

test('configuration jump targets the selected device when the shell grants engineering access', async ({ page }) => {
  await installFixture(page)
  await mountDeviceMonitor(page, { engineering: true })
  await page.getByRole('article', { name: /同名 PCS 设备卡片/ }).first().getByRole('button', { name: '查看详情' }).click()
  await page.getByRole('dialog', { name: '同名 PCS' }).getByRole('button', { name: '配置此设备' }).click()
  await expect.poll(() => page.evaluate(() => (window as Window & { __engineeringNodeId?: string }).__engineeringNodeId)).toBe('device-1')
})
