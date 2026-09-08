import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildWorkbenchSlots,
  compatibleSlotEntities,
  controlCommandPresentation,
  describeWorkbenchSlotError,
  fixedEnergyFlow,
  isConfirmationExpired,
} from './workbenchSlotsModel.ts'

const entity = (overrides = {}) => ({
  entity_instance_id: 'entity-pv',
  node_id: 'node-pv',
  node_name: '1# 光伏逆变器',
  definition_id: 'pv.active_power',
  display_name: '光伏有功功率',
  data_type: 'float',
  unit: 'kW',
  direction: 'R',
  status: 'available',
  value: 0,
  observed_at: '2026-09-08T08:00:00Z',
  quality: 192,
  ...overrides,
})

test('five fixed slots keep product order and a real GOOD zero remains current', () => {
  const slots = buildWorkbenchSlots([
    { id: 'pv-power', label: '光伏功率', binding_mode: 'exact', reason: '唯一标准定义', entity: entity() },
  ], [{
    entityInstanceId: 'entity-pv',
    observation: {
      value: 0, unit: 'kW', quality: 192, reason: null,
      observed_at: '2026-09-08T08:00:01Z', value_observed_at: '2026-09-08T08:00:00Z',
    },
    nodeCurrent: true,
  }])

  assert.deepEqual(slots.map((slot) => slot.id), [
    'site-power', 'pv-power', 'storage-power', 'storage-soc', 'charging-power',
  ])
  assert.equal(slots[1].reading.kind, 'current')
  assert.equal(slots[1].reading.valueText, '0.0')
  assert.equal(slots[1].reading.unit, 'kW')
  assert.equal(slots[0].reading.kind, 'missing')
  assert.equal(slots[0].reading.valueText, '—')
  assert.match(slots[0].reason, /未返回/)
})

test('committed runtime evidence replaces the workbench snapshot and disconnect downgrades it', () => {
  const kpis = [
    { id: 'pv-power', label: '光伏功率', binding_mode: 'exact', reason: '唯一标准定义', entity: entity({ value: 999 }) },
  ]
  const observation = {
    value: 15, unit: 'kW', quality: 192, reason: null,
    observed_at: '2026-09-08T08:00:03Z', value_observed_at: '2026-09-08T08:00:02Z',
  }

  const current = buildWorkbenchSlots(kpis, [{ entityInstanceId: 'entity-pv', observation, nodeCurrent: true }])
  assert.equal(current[1].reading.kind, 'current')
  assert.equal(current[1].reading.valueText, '15.0')
  assert.equal(current[1].reading.observedAt, '2026-09-08T08:00:02Z')

  const disconnected = buildWorkbenchSlots(kpis, [{
    entityInstanceId: 'entity-pv', observation, nodeCurrent: false, reason: '实时数据连接已断开，正在重连',
  }])
  assert.equal(disconnected[1].reading.kind, 'last')
  assert.equal(disconnected[1].reading.valueText, '15.0')
  assert.match(disconnected[1].reading.reason, /连接已断开/)
})

test('a committed runtime path without the matching observation never promotes the workbench snapshot', () => {
  const slots = buildWorkbenchSlots([
    { id: 'pv-power', label: '光伏功率', binding_mode: 'exact', reason: '唯一标准定义', entity: entity({ value: 15 }) },
  ], [{
    entityInstanceId: 'entity-pv', observation: null, nodeCurrent: true,
  }])

  assert.equal(slots[1].reading.kind, 'last')
  assert.equal(slots[1].reading.valueText, '15.0')
  assert.match(slots[1].reading.reason, /已提交实时帧缺少.*L2 观测/)
})

test('workbench GET refresh failure prevents committed evidence from being labelled current', () => {
  const slots = buildWorkbenchSlots([
    { id: 'pv-power', label: '光伏功率', binding_mode: 'exact', reason: '唯一标准定义', entity: entity({ value: 999 }) },
  ], [{
    entityInstanceId: 'entity-pv',
    observation: {
      value: 15, unit: 'kW', quality: 192, reason: null,
      observed_at: '2026-09-08T08:00:03Z', value_observed_at: '2026-09-08T08:00:02Z',
    },
    nodeCurrent: true,
  }], false)

  assert.equal(slots[1].reading.kind, 'last')
  assert.equal(slots[1].reading.valueText, '15.0')
  assert.match(slots[1].reading.reason, /工作台刷新失败/)
})

test('unconfigured, ambiguous, unavailable, bad quality, and non-numeric readings never become fake zero', () => {
  const slots = buildWorkbenchSlots([
    { id: 'site-power', label: '站点功率', binding_mode: 'unconfigured', reason: '未配置', entity: null },
    { id: 'pv-power', label: '光伏功率', binding_mode: 'ambiguous', reason: '存在多个候选', entity: null },
    { id: 'storage-power', label: '储能功率', binding_mode: 'manual', reason: '人工绑定目标不可用', entity: entity({ entity_instance_id: 'storage', status: 'unavailable', code: 'ENTITY_DATA_STALE', value: 12 }) },
    { id: 'storage-soc', label: '储能 SOC', binding_mode: 'exact', reason: '唯一标准定义', entity: entity({ entity_instance_id: 'soc', unit: '%', quality: 64, value: 62 }) },
    { id: 'charging-power', label: '充电功率', binding_mode: 'exact', reason: '唯一标准定义', entity: entity({ entity_instance_id: 'charger', value: '12.5' }) },
  ])

  assert.deepEqual(slots.map((slot) => slot.reading.kind), ['missing', 'missing', 'last', 'last', 'last'])
  assert.deepEqual(slots.map((slot) => slot.reading.valueText), ['—', '—', '12.0', '62.0', '12.5'])
  assert.equal(slots.some((slot) => slot.reading.kind !== 'current' && slot.reading.valueText === '0.0'), false)
})

test('energy topology stays neutral without current runtime evidence', () => {
  const flow = fixedEnergyFlow(buildWorkbenchSlots([
    { id: 'pv-power', label: '光伏功率', binding_mode: 'exact', reason: '唯一标准定义', entity: entity({ value: 89.2 }) },
    { id: 'storage-power', label: '储能功率', binding_mode: 'exact', reason: '唯一标准定义', entity: entity({ entity_instance_id: 'storage', value: -26.8 }) },
  ]))

  assert.deepEqual(flow.links.map((link) => link.direction), ['neutral', 'neutral', 'neutral', 'neutral'])
  assert.match(flow.reason, /超时/)
})

test('current signed storage and grid power reverse flow; zero and stale readings stop it', () => {
  for (const [id, value, current, expected] of [
    ['storage-power', -26.8, true, 'from-bus'],
    ['storage-power', 26.8, true, 'to-bus'],
    ['site-power', -10, true, 'from-bus'],
    ['site-power', 10, true, 'to-bus'],
    ['pv-power', 10, true, 'to-bus'],
    ['storage-power', 0, true, 'neutral'],
    ['storage-power', -10, false, 'neutral'],
  ]) {
    const slots = buildWorkbenchSlots([
      { id, binding_mode: 'exact', entity: entity({ value }), reason: '', label: id },
    ], [{ entityInstanceId: 'entity-pv', nodeCurrent: current,
      observation: { value, quality: 192, unit: 'kW', observed_at: '2026-09-08T08:00:00Z' } }])
    assert.equal(fixedEnergyFlow(slots).links.find((link) => link.from === id).direction, expected)
  }
})

test('binding candidates use exact type and unit compatibility rather than names', () => {
  const descriptors = [
    { id: 'good', data_type: 'float', unit: 'kW', display_name: '随便一个名称' },
    { id: 'int-good', data_type: 'int', unit: 'kW', display_name: '充电功率' },
    { id: 'wrong-unit', data_type: 'float', unit: 'W', display_name: '站点功率' },
    { id: 'wrong-type', data_type: 'bool', unit: 'kW', display_name: '光伏功率' },
    { id: 'soc', data_type: 'float', unit: '%', display_name: '完全不含 SOC' },
  ]

  assert.deepEqual(compatibleSlotEntities('pv-power', descriptors).map((item) => item.id), ['good', 'int-good'])
  assert.deepEqual(compatibleSlotEntities('storage-soc', descriptors).map((item) => item.id), ['soc'])
})

test('control presentation distinguishes acceptance, confirmed readback, and terminal failures', () => {
  assert.deepEqual(controlCommandPresentation({ status: 'dispatched', code: 'CONTROL_DISPATCHED' }), {
    tone: 'waiting', label: '等待设备回读', terminal: false,
  })
  assert.deepEqual(controlCommandPresentation({ status: 'readback_confirmed', code: 'CONTROL_READBACK_CONFIRMED' }), {
    tone: 'success', label: '回读已确认', terminal: true,
  })
  assert.equal(controlCommandPresentation({ status: 'timeout', code: 'CONTROL_READBACK_TIMEOUT' }).tone, 'danger')
  assert.equal(controlCommandPresentation({ status: 'mismatch', code: 'CONTROL_READBACK_MISMATCH' }).label, '回读不一致')
  assert.equal(controlCommandPresentation({ status: 'failed', code: 'CONTROL_DISPATCH_FAILED' }).terminal, true)
})

test('confirmation expiry is checked against an injected clock without extending the server deadline', () => {
  assert.equal(isConfirmationExpired('2026-09-08T08:00:00Z', Date.parse('2026-09-08T07:59:59Z')), false)
  assert.equal(isConfirmationExpired('2026-09-08T08:00:00Z', Date.parse('2026-09-08T08:00:00Z')), true)
  assert.equal(isConfirmationExpired('not-a-time', Date.parse('2026-09-08T08:00:00Z')), true)
})

test('slot conflicts and runtime fencing errors have actionable local copy', () => {
  assert.equal(describeWorkbenchSlotError({ code: 'CONFIGURATION_REVISION_STALE', message: 'stale' }), '配置已被其他操作更新，请关闭弹窗、刷新首页后重试。')
  assert.equal(describeWorkbenchSlotError({ code: 'CONFIGURATION_RUNTIME_BUSY', message: 'busy' }), '运行配置正在切换，请稍后重试；当前绑定未按成功处理。')
  assert.equal(describeWorkbenchSlotError({ code: 'WORKBENCH_SLOT_ENTITY_NOT_AVAILABLE', message: 'missing' }), '所选 L2 已失效或不再可用，请刷新实体目录后重新选择。')
  assert.equal(describeWorkbenchSlotError({ code: 'UNEXPECTED', message: '后端提供的具体错误' }), '后端提供的具体错误')
})
