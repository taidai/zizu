import { expect, test, type Page, type Route } from '@playwright/test'

const entities = {
  site: {
    entity_instance_id: 'entity-site', node_id: 'node-site', node_name: '并网电表',
    definition_id: 'site.active_power', display_name: '站点有功功率', data_type: 'float', unit: 'kW',
    direction: 'R', status: 'available', value: 0, quality: 64, observed_at: '2026-09-08T08:00:00Z',
  },
  pv: {
    entity_instance_id: 'entity-pv', node_id: 'node-pv', node_name: '1# 光伏逆变器',
    definition_id: 'pv.active_power', display_name: '光伏有功功率', data_type: 'float', unit: 'kW',
    direction: 'R', status: 'available', value: 89.2, quality: 192, observed_at: '2026-09-08T08:00:00Z',
  },
  soc: {
    entity_instance_id: 'entity-soc', node_id: 'node-bms', node_name: '1# BMS',
    definition_id: 'storage.soc', display_name: '储能 SOC', data_type: 'float', unit: '%',
    direction: 'R', status: 'available', value: 62, quality: 64, observed_at: '2026-09-08T07:58:00Z',
  },
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function installFixture(page: Page, role: 'operator' | 'engineer' = 'operator') {
  const writes: string[] = []
  let releaseDelayedWorkbench: (() => void) | null = null
  const state = {
    writes,
    historyRequests: 0,
    trunkRequests: 0,
    workbenchReads: 0,
    failWorkbenchRead: false,
    configurationRevision: 12,
    storagePowerEntity: null as typeof entities.pv | null,
    omitRuntimeObservationNodeIds: new Set<string>(),
    delayNextWorkbenchRead: false,
    delayedWorkbenchCompleted: 0,
    releaseDelayedWorkbench: () => {
      releaseDelayedWorkbench?.()
      releaseDelayedWorkbench = null
    },
  }
  const user = { id: `${role}-1`, username: `tablet-${role}`, role }
  await page.addInitScript((currentUser) => {
    sessionStorage.setItem('zizu.auth.session.v1', JSON.stringify({
      accessToken: 'runtime-fixture-token', expiresAt: '2099-01-01T00:00:00Z', user: currentUser,
    }))
  }, user)
  await page.addInitScript(() => {
    const sockets: Array<{ onmessage: ((event: { data: string }) => void) | null; onopen: (() => void) | null; onclose: ((event: { code: number }) => void) | null; readyState: number }> = []
    class RuntimeSocket {
      static readonly OPEN = 1
      static readonly CLOSED = 3
      onopen: (() => void) | null = null
      onmessage: ((event: { data: string }) => void) | null = null
      onerror: (() => void) | null = null
      onclose: ((event: { code: number }) => void) | null = null
      readyState = 0
      constructor() {
        sockets.push(this)
        setTimeout(() => { this.readyState = 1; this.onopen?.() }, 0)
      }
      send(raw: string) {
        const payload = JSON.parse(raw)
        setTimeout(() => {
          if (payload.authenticate) this.onmessage?.({ data: JSON.stringify({ type: 'authenticated' }) })
          if (payload.subscribe) this.onmessage?.({ data: JSON.stringify({ type: 'subscribed' }) })
        }, 0)
      }
      close() { this.readyState = 3 }
    }
    Object.defineProperty(window, 'WebSocket', { configurable: true, value: RuntimeSocket })
    ;(window as unknown as { disconnectRuntime: () => void }).disconnectRuntime = () => {
      for (const socket of sockets) {
        if (socket.readyState === 3) continue
        socket.readyState = 3
        socket.onclose?.({ code: 1006 })
      }
    }
  })
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() !== 'GET') writes.push(`${request.method()} ${url.pathname}`)
    if (url.pathname.endsWith('/auth/me')) return fulfillJson(route, { user })
    if (url.pathname.endsWith('/health')) return fulfillJson(route, {
      status: 'healthy', version: 'test', uptime_seconds: 1,
      components: { timescaledb: { status: 'connected' }, mqtt: { status: 'connected' }, neuron: { status: 'connected' } },
      pipeline: { status: 'running', messages_received: 1, points_written_db: 1, last_message_at: '2026-09-08T08:00:00Z' },
    })
    if (url.pathname.endsWith('/entity-instances')) return fulfillJson(route, {
      items: Object.values(entities).map((item) => ({
        id: item.entity_instance_id, node_id: item.node_id, node_type: 'meter', node_display_name: item.node_name,
        definition_id: item.definition_id, display_name: item.display_name, data_type: item.data_type,
        unit: item.unit, direction: item.direction, freshness_seconds: 3600, confirmed: true,
      })), total: 3,
    })
    if (url.pathname.endsWith('/runtime/frame-snapshot')) {
      const nodeId = url.searchParams.get('node_id') || ''
      const item = Object.values(entities).find((candidate) => candidate.node_id === nodeId)
      return fulfillJson(route, {
        type: 'frame_snapshot', node_id: nodeId, cursor: `${nodeId}:1`, frame_sequence: 1,
        frame_time: '2026-09-08T08:00:03Z', configuration_revision: state.configurationRevision,
        frame_status: 'COMPLETE', failure: null, backlog_frames: 0, l0: [],
        l2: item && !state.omitRuntimeObservationNodeIds.has(nodeId) ? [{
          entity_instance_id: item.entity_instance_id, node_id: item.node_id,
          definition_id: item.definition_id, display_name: item.display_name, data_type: item.data_type,
          value: item.entity_instance_id === 'entity-pv' ? 15 : item.value, unit: item.unit,
          quality: item.quality, reason: item.quality === 192 ? null : '源点质量超时',
          observed_at: '2026-09-08T08:00:03Z', value_observed_at: '2026-09-08T08:00:02Z',
          received_at: '2026-09-08T08:00:03Z', calculated_at: '2026-09-08T08:00:03Z',
          processing_revision_id: 'processing-1', configuration_revision: state.configurationRevision,
          source_digest: 'sha256:fixture', frame_sequence: 1,
        }] : [],
      })
    }
    if (url.pathname.endsWith('/auth/ws-ticket')) return fulfillJson(route, { ticket: `ticket-${Date.now()}` }, 201)
    if (url.pathname.endsWith('/entity-instances/entity-pv/history')) {
      state.historyRequests += 1
      return fulfillJson(route, { items: [] })
    }
    if (url.pathname.endsWith('/nodes/node-pv/data-trunk')) {
      state.trunkRequests += 1
      return fulfillJson(route, {
        node_id: 'node-pv', l0: [],
        l1_summary: { installed: true, revision_id: 'processing-1', configuration_revision: state.configurationRevision, output_count: 1, source_summary: [] },
        l2: [{ output_key: 'pv.active_power', entity_instance_id: 'entity-pv', processing_kind: 'identity', source_summary: [{ input_id: 'input-pv', source_kind: 'l2', source_key: 'upstream-pv' }] }],
      })
    }
    if (url.pathname.endsWith('/alarms/counts')) return fulfillJson(route, { counts: { 'node-pv': 2 } })
    if (url.pathname.endsWith('/alarm-events')) return fulfillJson(route, {
      items: [{ id: 'alarm-1', definition_id: 'alarm.communication', entity_instance_id: 'entity-pv', state: 'active_unacknowledged', severity: 'MAJOR', pending_at: '2026-09-08T07:59:00Z', active_at: '2026-09-08T07:59:10Z', acknowledged_at: null, acknowledged_by: null, recovered_at: null, node_name: '1# 光伏逆变器', entity_name: '光伏有功功率', alarm_name: '通信中断', duration_seconds: 50, archived_at: null, archived_by: null }],
      total: 1, page: 1, page_size: 10, total_pages: 1,
      summary: { active: 1, unacknowledged: 1, critical: 0 },
    })
    if (url.pathname.endsWith('/dispatch-strategies')) return fulfillJson(route, { strategies: [{ id: 'strategy-1', name: '午间充电', description: null, active_revision_id: 'rev-1', enabled: true, runtime_health: 'READY', last_trigger_key: 'frame:12', last_evaluated_at: '2026-09-08T08:00:00Z', last_desired: { charge: 26.8 }, last_actual: { charge: 26.8 }, last_evidence: null, failure_code: null, created_at: '2026-09-08T07:00:00Z', updated_at: '2026-09-08T08:00:00Z', draft: null, active_revision: null, published_revision: null }] })
    if (url.pathname.endsWith('/ems-workbench')) {
      state.workbenchReads += 1
      const payload = {
      workbench_id: 'fixed-light-storage-charging', configuration_revision: state.configurationRevision,
      navigation: [{ id: 'overview', label: '总览' }, { id: 'trends', label: '趋势' }, { id: 'alarms', label: '告警' }, { id: 'controls', label: '控制' }],
      groups: [],
      kpis: [
        { id: 'site-power', label: '站点功率', binding_mode: 'exact', reason: '唯一标准定义', entity: entities.site },
        { id: 'pv-power', label: '光伏功率', binding_mode: 'exact', reason: '唯一标准定义', entity: entities.pv },
        { id: 'storage-power', label: '储能功率', binding_mode: state.storagePowerEntity ? 'manual' : 'ambiguous', reason: state.storagePowerEntity ? '人工绑定' : '找到 2 个精确候选，需人工选择', entity: state.storagePowerEntity },
        { id: 'storage-soc', label: '储能 SOC', binding_mode: 'exact', reason: '唯一标准定义', entity: entities.soc },
        { id: 'charging-power', label: '充电功率', binding_mode: 'unconfigured', reason: '未找到精确标准定义', entity: null },
      ],
      trends: [], alarms: { visible: true }, controls: { visible: false, entities: [] },
      }
      if (state.failWorkbenchRead) return fulfillJson(route, { detail: { message: '正式工作台暂不可用' } }, 503)
      if (state.delayNextWorkbenchRead) {
        state.delayNextWorkbenchRead = false
        await new Promise<void>((resolve) => { releaseDelayedWorkbench = resolve })
        state.delayedWorkbenchCompleted += 1
      }
      return fulfillJson(route, payload)
    }
    return fulfillJson(route, { items: [], total: 0 })
  })
  return state
}

for (const viewport of [{ width: 1024, height: 768 }, { width: 1280, height: 800 }]) {
  test(`formal overview composes five truthful slots at ${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    const fixture = await installFixture(page)
    await page.setViewportSize(viewport)
    await page.goto('/', { waitUntil: 'networkidle' })

    await expect(page.getByRole('heading', { name: '运行总览', exact: true })).toBeVisible()
    await expect(page.locator('[data-workbench-slot]')).toHaveCount(5)
    await expect(page.locator('[data-workbench-slot="site-power"]')).toContainText('0.0')
    await expect(page.locator('[data-workbench-slot="storage-power"]')).toContainText('需人工选择')
    await expect(page.locator('[data-workbench-slot="storage-soc"]')).toContainText('最后值')
    await expect(page.locator('[data-workbench-slot="charging-power"]')).toContainText('未配置')
    const energy = page.getByRole('region', { name: '站点能流' })
    await expect(energy).toContainText('电网＋供电／−返送')
    const pv = await energy.locator('.workbench-flow-node--pv').boundingBox()
    const grid = await energy.locator('.workbench-flow-node--site').boundingBox()
    const storage = await energy.locator('.workbench-flow-node--storage').boundingBox()
    expect(pv!.x).toBeLessThan(storage!.x)
    expect(grid!.x).toBe(pv!.x)
    expect(grid!.y).toBeGreaterThan(pv!.y)
    await expect(energy.locator('[data-flow-slot="site-power"]')).toHaveAttribute('data-direction', 'neutral')
    await expect(page.getByRole('region', { name: '待处理告警' })).toContainText('通信中断')
    await expect(page.getByRole('region', { name: '调度摘要' })).toContainText('午间充电')
    await expect(page.getByRole('button', { name: /配置首页指标/ })).toHaveCount(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
    expect(fixture.writes.filter((request) => !request.endsWith('/auth/ws-ticket'))).toEqual([])
    await page.screenshot({ path: testInfo.outputPath(`overview-${viewport.width}x${viewport.height}.png`), fullPage: true })
  })

  test(`workbench key touch targets are at least 44px at ${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    // Break caught: desktop/mobile CSS shrinks key navigation hit areas below 44px.
    await installFixture(page, 'engineer')
    await page.setViewportSize(viewport)
    await page.goto('/', { waitUntil: 'networkidle' })
    const navigationActions = page.getByRole('button', { name: /^(查看设备|查看告警|前往工程配置)/ })
    await expect(navigationActions).toHaveCount(3)
    for (const action of await navigationActions.all()) {
      await expect(action).toBeVisible()
      expect.soft((await action.boundingBox())?.height, `${viewport.width}px ${await action.innerText()} touch height`).toBeGreaterThanOrEqual(44)
    }
    await page.screenshot({ path: testInfo.outputPath(`touch-targets-${viewport.width}x${viewport.height}.png`), fullPage: true })
  })
}

test('metric opens the existing committed-L2 detail instead of exposing frame evidence on the overview', async ({ page }) => {
  const fixture = await installFixture(page)
  await page.goto('/', { waitUntil: 'networkidle' })
  await page.locator('[data-workbench-slot="pv-power"]').click()
  await expect(page.getByRole('dialog', { name: '光伏有功功率' })).toBeVisible()
  await expect(page.getByRole('dialog', { name: '光伏有功功率' })).toContainText('历史')
  await expect(page.getByRole('dialog', { name: '光伏有功功率' })).toContainText('来源与帧证据')
  await expect.poll(() => fixture.historyRequests).toBeGreaterThan(0)
  await expect.poll(() => fixture.trunkRequests).toBeGreaterThan(0)
  await expect(page.locator('.workbench-dashboard')).not.toContainText('sha256:')
})

test('a current committed value downgrades after workbench refresh failure and stream disconnect', async ({ page }) => {
  const fixture = await installFixture(page)
  await page.goto('/', { waitUntil: 'networkidle' })
  const pv = page.locator('[data-workbench-slot="pv-power"]')
  await expect(pv).toContainText('15.0')
  await expect(pv).toContainText('当前值')

  fixture.failWorkbenchRead = true
  await page.evaluate(() => (window as unknown as { disconnectRuntime: () => void }).disconnectRuntime())
  await page.getByRole('button', { name: '刷新', exact: true }).click()

  await expect(page.getByRole('alert')).toContainText('正式工作台暂不可用')
  await expect(pv.locator('.workbench-metric__state')).toHaveText('最后值（非当前）')
})

test('a complete runtime frame missing the bound L2 observation fails closed', async ({ page }) => {
  const fixture = await installFixture(page)
  fixture.omitRuntimeObservationNodeIds.add('node-pv')
  await page.goto('/', { waitUntil: 'networkidle' })

  const pv = page.locator('[data-workbench-slot="pv-power"]')
  await expect(pv).toContainText('89.2')
  await expect(pv.locator('.workbench-metric__state')).toHaveText('最后值（非当前）')
  await expect(pv).toContainText(/已提交实时帧缺少.*L2 观测/)
})

test('engineer can bind and clear a fixed slot with current revision and stable idempotency headers', async ({ page }) => {
  const fixture = await installFixture(page, 'engineer')
  const calls: Array<{ body: unknown; key: string | null }> = []
  await page.route('**/api/v1/ems-workbench/slots/storage-power', async (route) => {
    calls.push({ body: route.request().postDataJSON(), key: await route.request().headerValue('Idempotency-Key') })
    fixture.configurationRevision = 12 + calls.length
    fixture.storagePowerEntity = calls.length === 1 ? entities.pv : null
    await fulfillJson(route, { slot_key: 'storage-power', entity_instance_id: fixture.storagePowerEntity?.entity_instance_id || null, configuration_revision: fixture.configurationRevision, replayed: false })
  })
  await page.goto('/', { waitUntil: 'networkidle' })

  await page.getByRole('button', { name: /配置首页指标/ }).click()
  const dialog = page.getByRole('dialog', { name: '配置首页指标' })
  await expect(dialog).toBeVisible()
  await dialog.getByLabel('储能功率绑定').selectOption('entity-pv')
  await dialog.getByRole('button', { name: '保存储能功率' }).click()
  await expect(dialog.getByRole('status')).toContainText('配置修订 13')
  expect(calls[0].body).toEqual({ entity_instance_id: 'entity-pv', base_configuration_revision: 12 })
  expect(calls[0].key).toBeTruthy()

  await dialog.getByRole('button', { name: '关闭' }).click()
  await expect(page.locator('[data-workbench-slot="storage-power"]')).toContainText('1# 光伏逆变器')
  await page.getByRole('button', { name: /配置首页指标/ }).click()
  const reopened = page.getByRole('dialog', { name: '配置首页指标' })
  await expect(reopened.getByLabel('储能功率绑定')).toHaveValue('entity-pv')

  await reopened.getByRole('button', { name: '清除储能功率绑定' }).click()
  await expect(reopened.getByRole('status')).toContainText('配置修订 14')
  expect(calls[1].body).toEqual({ entity_instance_id: null, base_configuration_revision: 13 })
  expect(calls[1].key).toBeTruthy()
})

test('a successful slot write followed by refresh failure never paints the local selection as committed', async ({ page }) => {
  const fixture = await installFixture(page, 'engineer')
  await page.route('**/api/v1/ems-workbench/slots/storage-power', async (route) => {
    fixture.configurationRevision = 13
    fixture.storagePowerEntity = entities.pv
    fixture.failWorkbenchRead = true
    await fulfillJson(route, { slot_key: 'storage-power', entity_instance_id: 'entity-pv', configuration_revision: 13, replayed: false })
  })
  await page.goto('/', { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: /配置首页指标/ }).click()
  const dialog = page.getByRole('dialog', { name: '配置首页指标' })
  await dialog.getByLabel('储能功率绑定').selectOption('entity-pv')
  await dialog.getByRole('button', { name: '保存储能功率' }).click()

  await expect(dialog.getByRole('alert')).toContainText(/保存已受理.*读取最新工作台失败/)
  await expect(dialog.getByRole('status')).toHaveCount(0)
  await expect(page.locator('[data-workbench-slot="storage-power"]')).toContainText('需人工选择')
  await expect(page.locator('[data-workbench-slot="storage-power"]')).not.toContainText('1# 光伏逆变器')
})

test('an older slow refresh cannot overwrite the workbench reloaded after a slot save', async ({ page }) => {
  const fixture = await installFixture(page, 'engineer')
  await page.route('**/api/v1/ems-workbench/slots/storage-power', async (route) => {
    fixture.configurationRevision = 13
    fixture.storagePowerEntity = entities.pv
    await fulfillJson(route, { slot_key: 'storage-power', entity_instance_id: 'entity-pv', configuration_revision: 13, replayed: false })
  })
  await page.goto('/', { waitUntil: 'networkidle' })

  const readsBeforeRefresh = fixture.workbenchReads
  fixture.delayNextWorkbenchRead = true
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect.poll(() => fixture.workbenchReads).toBeGreaterThan(readsBeforeRefresh)
  await page.getByRole('button', { name: /配置首页指标/ }).click()
  const dialog = page.getByRole('dialog', { name: '配置首页指标' })
  await dialog.getByLabel('储能功率绑定').selectOption('entity-pv')
  await dialog.getByRole('button', { name: '保存储能功率' }).click()
  await expect(dialog.getByRole('status')).toContainText('配置修订 13')

  fixture.releaseDelayedWorkbench()
  await expect.poll(() => fixture.delayedWorkbenchCompleted).toBe(1)
  await dialog.getByRole('button', { name: '关闭' }).click()
  await expect(page.locator('[data-workbench-slot="storage-power"]')).toContainText('1# 光伏逆变器')
  await page.getByRole('button', { name: /配置首页指标/ }).click()
  const reopened = page.getByRole('dialog', { name: '配置首页指标' })
  await expect(reopened).toContainText('当前配置修订 13')
  await expect(reopened.getByLabel('储能功率绑定')).toHaveValue('entity-pv')
})

test('flow nodes expose current-or-last state, quality reason, and last-value time', async ({ page }) => {
  await installFixture(page)
  await page.goto('/', { waitUntil: 'networkidle' })
  const flow = page.getByRole('region', { name: '站点能流' })
  await expect(flow).toContainText('当前值')
  await expect(flow).toContainText('最后值（非当前）')
  await expect(flow).toContainText('质量超时')
  await expect(flow).toContainText('源点质量超时')
  await expect(flow).toContainText('2026/9/8 16:00:02')
})

test('slot revision conflicts stay in the dialog and never look saved', async ({ page }) => {
  await installFixture(page, 'engineer')
  await page.route('**/api/v1/ems-workbench/slots/storage-power', (route) => fulfillJson(route, {
    detail: { code: 'CONFIGURATION_REVISION_STALE', message: 'stale revision' },
  }, 409))
  await page.goto('/', { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: /配置首页指标/ }).click()
  const dialog = page.getByRole('dialog', { name: '配置首页指标' })
  await dialog.getByLabel('储能功率绑定').selectOption('entity-pv')
  await dialog.getByRole('button', { name: '保存储能功率' }).click()
  await expect(dialog.getByRole('alert')).toContainText('配置已被其他操作更新，请关闭弹窗、刷新首页后重试')
  await expect(dialog.getByRole('status')).toHaveCount(0)
  await expect(dialog).toBeVisible()
})
