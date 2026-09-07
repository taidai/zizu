import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildDeviceMonitorPage,
  buildRuntimeNodes,
  entityHistoryModel,
  numericHistorySegments,
  paginateEntityDetails,
  runtimeEntityReading,
} from './runtimeModel.ts'

const descriptors = [
  {
    id: 'entity-pcs-a-power',
    node_id: 'node-pcs-a',
    node_type: 'PCS',
    node_display_name: '1# PCS',
    definition_id: 'pcs.active_power',
    display_name: '有功功率',
    data_type: 'float',
    unit: 'kW',
    direction: 'R',
    freshness_seconds: 10,
    confirmed: true,
  },
  {
    id: 'entity-pcs-b-power',
    node_id: 'node-pcs-b',
    node_type: 'PCS',
    node_display_name: '2# PCS',
    definition_id: 'pcs.active_power',
    display_name: '有功功率',
    data_type: 'float',
    unit: 'W',
    direction: 'R',
    freshness_seconds: 10,
    confirmed: true,
  },
]

function completeFrame(nodeId, entity) {
  return {
    type: 'frame_snapshot',
    node_id: nodeId,
    cursor: `cursor-${nodeId}`,
    frame_sequence: 41,
    frame_time: '2026-09-07T01:02:03.000Z',
    configuration_revision: 88,
    frame_status: 'COMPLETE',
    failure: null,
    backlog_frames: 0,
    l0: [],
    l2: [{
      entity_instance_id: entity.id,
      node_id: entity.node_id,
      definition_id: entity.definition_id,
      display_name: entity.display_name,
      data_type: entity.data_type,
      value: entity.id === 'entity-pcs-a-power' ? 0 : 9000,
      unit: entity.unit,
      quality: 192,
      reason: null,
      observed_at: '2026-09-07T01:02:03.000Z',
      value_observed_at: '2026-09-07T01:02:03.000Z',
      received_at: '2026-09-07T01:02:03.000Z',
      calculated_at: '2026-09-07T01:02:03.000Z',
      processing_revision_id: 'processing-revision-1',
      configuration_revision: 88,
      source_digest: 'sha256:frame-evidence',
      frame_sequence: 41,
    }],
  }
}

test('runtime nodes associate same-definition entities only by instance id and validated node/definition', () => {
  const frames = new Map([
    ['node-pcs-b', completeFrame('node-pcs-b', descriptors[1])],
    ['node-pcs-a', completeFrame('node-pcs-a', descriptors[0])],
  ])

  const nodes = buildRuntimeNodes(descriptors, frames)

  assert.deepEqual(nodes.map((node) => [node.nodeId, node.entities[0].observation?.value]), [
    ['node-pcs-a', 0],
    ['node-pcs-b', 9000],
  ])

  const wrongIdentity = completeFrame('node-pcs-a', descriptors[0])
  wrongIdentity.l2[0] = {
    ...wrongIdentity.l2[0],
    entity_instance_id: 'entity-pcs-b-power',
    node_id: 'node-pcs-b',
  }
  assert.equal(buildRuntimeNodes(descriptors, new Map([['node-pcs-a', wrongIdentity]]))[0].entities[0].observation, null)
})

test('zero is current while bad quality and a disconnected stream expose only timestamped last evidence', () => {
  const zero = completeFrame('node-pcs-a', descriptors[0]).l2[0]
  assert.deepEqual(runtimeEntityReading(zero, true), {
    kind: 'current',
    value: 0,
    quality: 192,
    observedAt: '2026-09-07T01:02:03.000Z',
    valueObservedAt: '2026-09-07T01:02:03.000Z',
  })

  const bad = { ...zero, value: 12.5, quality: 64, observed_at: '2026-09-07T01:04:00.000Z' }
  assert.deepEqual(runtimeEntityReading(bad, true), {
    kind: 'last',
    value: 12.5,
    quality: 64,
    observedAt: '2026-09-07T01:04:00.000Z',
    valueObservedAt: '2026-09-07T01:02:03.000Z',
  })
  assert.equal(runtimeEntityReading(zero, false).kind, 'last')
})

test('history stays scoped to one entity and unit and bad points split numeric lines', () => {
  const points = [
    { event_id: 'a-1', entity_instance_id: 'entity-pcs-a-power', definition_id: 'pcs.active_power', value: 1, data_type: 'float', unit: 'kW', quality: 192, reason: null, observed_at: '2026-09-07T01:00:00.000Z', age_ms: 0, processing_revision_id: 'pr-1', configuration_revision: 88 },
    { event_id: 'a-2', entity_instance_id: 'entity-pcs-a-power', definition_id: 'pcs.active_power', value: 2, data_type: 'float', unit: 'kW', quality: 0, reason: 'INPUT_BAD', observed_at: '2026-09-07T01:01:00.000Z', age_ms: 0, processing_revision_id: 'pr-1', configuration_revision: 88 },
    { event_id: 'a-3', entity_instance_id: 'entity-pcs-a-power', definition_id: 'pcs.active_power', value: 3, data_type: 'float', unit: 'kW', quality: 192, reason: null, observed_at: '2026-09-07T01:02:00.000Z', age_ms: 0, processing_revision_id: 'pr-1', configuration_revision: 88 },
    { event_id: 'b-1', entity_instance_id: 'entity-pcs-b-power', definition_id: 'pcs.active_power', value: 9000, data_type: 'float', unit: 'W', quality: 192, reason: null, observed_at: '2026-09-07T01:03:00.000Z', age_ms: 0, processing_revision_id: 'pr-1', configuration_revision: 88 },
  ]

  const history = entityHistoryModel(descriptors[0], points)

  assert.equal(history.unit, 'kW')
  assert.deepEqual(history.points.map((point) => point.value), [1, 2, 3])
  assert.deepEqual(history.numericSegments, [
    [{ time: Date.parse('2026-09-07T01:00:00.000Z'), value: 1 }],
    [{ time: Date.parse('2026-09-07T01:02:00.000Z'), value: 3 }],
  ])
})

test('numericHistorySegments rejects invalid time/value and does not bridge non-GOOD evidence', () => {
  assert.deepEqual(numericHistorySegments([
    { observed_at: '2026-09-07T01:00:00.000Z', value: 0, quality: 192 },
    { observed_at: 'bad-time', value: 1, quality: 192 },
    { observed_at: '2026-09-07T01:02:00.000Z', value: Number.NaN, quality: 192 },
    { observed_at: '2026-09-07T01:03:00.000Z', value: 3, quality: 64 },
    { observed_at: '2026-09-07T01:04:00.000Z', value: 4, quality: 192 },
  ]), [
    [{ time: Date.parse('2026-09-07T01:00:00.000Z'), value: 0 }],
    [{ time: Date.parse('2026-09-07T01:04:00.000Z'), value: 4 }],
  ])
})

test('device monitor pages six real nodes and keeps same-name identities and unconfigured nodes separate', () => {
  const nodes = Array.from({ length: 8 }, (_, index) => ({
    id: `device-${index + 1}`,
    name: index < 2 ? '同名 PCS' : `${index + 1}# 设备`,
    parent_id: 'site-1',
    layer: 4,
    node_type: index === 7 ? '' : 'PCS',
    sort_order: index,
    enabled: true,
    tag_count: 0,
  }))
  const entity = { ...descriptors[0], id: 'entity-only-device-2', node_id: 'device-2', node_display_name: '同名 PCS' }

  const first = buildDeviceMonitorPage({ nodes, descriptors: [entity], alarmCounts: { 'device-1': 3 }, query: '', category: '', onlyAlarms: false, page: 1 })
  const second = buildDeviceMonitorPage({ nodes, descriptors: [entity], alarmCounts: { 'device-1': 3 }, query: '', category: '', onlyAlarms: false, page: 2 })

  assert.equal(first.pageItems.length, 6)
  assert.deepEqual(first.activeNodeIds, ['device-1', 'device-2', 'device-3', 'device-4', 'device-5', 'device-6'])
  assert.deepEqual(second.activeNodeIds, ['device-7', 'device-8'])
  assert.equal(first.pageItems[0].entities.length, 0)
  assert.equal(first.pageItems[1].entities[0].id, 'entity-only-device-2')
  assert.equal(second.pageItems[1].category, '其他')
})

test('device alarm filtering preserves unresolved counts and blocks filtering when count evidence failed', () => {
  const nodes = [
    { id: 'device-a', name: '设备 A', parent_id: null, layer: 4, node_type: 'PCS', sort_order: 0, enabled: true, tag_count: 0 },
    { id: 'device-b', name: '设备 B', parent_id: null, layer: 4, node_type: 'PCS', sort_order: 1, enabled: true, tag_count: 0 },
  ]
  const counted = buildDeviceMonitorPage({ nodes, descriptors: [], alarmCounts: { 'device-a': 2 }, query: '', category: '', onlyAlarms: true, page: 1 })
  assert.deepEqual(counted.activeNodeIds, ['device-a'])
  assert.equal(counted.pageItems[0].alarmCount, 2)

  const failed = buildDeviceMonitorPage({ nodes, descriptors: [], alarmCounts: null, query: '', category: '', onlyAlarms: true, page: 1 })
  assert.equal(failed.alarmFilterBlocked, true)
  assert.deepEqual(failed.activeNodeIds, [])
  assert.equal(buildDeviceMonitorPage({ nodes, descriptors: [], alarmCounts: null, query: '', category: '', onlyAlarms: false, page: 1 }).pageItems[0].alarmCount, null)
})

test('device entity detail paginates 10 or 20 items and status histories stay non-numeric', () => {
  const entities = Array.from({ length: 23 }, (_, index) => ({ ...descriptors[0], id: `entity-${index + 1}` }))
  assert.deepEqual(paginateEntityDetails(entities, 2, 10).items.map((item) => item.id), [
    'entity-11', 'entity-12', 'entity-13', 'entity-14', 'entity-15',
    'entity-16', 'entity-17', 'entity-18', 'entity-19', 'entity-20',
  ])
  assert.equal(paginateEntityDetails(entities, 2, 20).items.length, 3)

  const status = { ...descriptors[0], id: 'entity-status', definition_id: 'pcs.running_state', data_type: 'bool', unit: null }
  const statusHistory = entityHistoryModel(status, [{
    event_id: 'status-1', entity_instance_id: 'entity-status', definition_id: 'pcs.running_state', value: false,
    data_type: 'bool', unit: null, quality: 192, reason: null, observed_at: '2026-09-07T01:00:00.000Z',
    age_ms: 0, processing_revision_id: 'pr-1', configuration_revision: 88,
  }])
  assert.deepEqual(statusHistory.points.map((point) => point.value), [false])
  assert.deepEqual(statusHistory.numericSegments, [])
})
