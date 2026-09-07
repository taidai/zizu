import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildRuntimeNodes,
  entityHistoryModel,
  numericHistorySegments,
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
