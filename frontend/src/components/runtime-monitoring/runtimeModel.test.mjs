import assert from 'node:assert/strict'
import test from 'node:test'
import * as runtimeModel from './runtimeModel.ts'

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

function provenanceFixture() {
  return {
    descriptor: descriptors[0],
    observation: completeFrame('node-pcs-a', descriptors[0]).l2[0],
    trunk: {
      node_id: 'node-pcs-a', l0: [{ source_id: 'tag-power', source_key: 'ActivePower', data_type: 'float', unit: 'W' }],
      l1_summary: { installed: true, revision_id: 'processing-revision-1', configuration_revision: 88, output_count: 1, source_summary: [] },
      l2: [{ output_key: 'power', entity_instance_id: descriptors[0].id, processing_kind: 'scale', source_summary: [
        { input_id: 'raw-power', source_kind: 'l0', source_key: 'ActivePower' },
      ] }],
    },
    l0: [{ tag_id: 'tag-power', node_id: 'node-pcs-a', name: 'ActivePower', display_name: '品牌原始功率',
      value: 0, unit: 'W', data_type: 'float', source_quality: 192, effective_quality: 64,
      source_timestamp: '2026-09-07T01:00:00Z', received_at: '2026-09-07T01:00:01Z',
      source_path: 'gateway/group/ActivePower', frame_sequence: 40 }],
  }
}

test('provenance joins the selected output to exact committed L0 identity and preserves zero and quality evidence', () => {
  assert.equal(typeof runtimeModel.entityProvenanceModel, 'function')
  const result = runtimeModel.entityProvenanceModel(provenanceFixture())
  assert.equal(result.error, null)
  assert.equal(result.processingKind, 'scale')
  assert.equal(result.revisionId, 'processing-revision-1')
  assert.equal(result.sources[0].point.value, 0)
  assert.equal(result.sources[0].point.unit, 'W')
  assert.equal(result.sources[0].point.source_quality, 192)
  assert.equal(result.sources[0].point.effective_quality, 64)
  assert.equal(result.sources[0].point.source_path, 'gateway/group/ActivePower')
  assert.equal(result.sources[0].point.source_timestamp, '2026-09-07T01:00:00Z')
  assert.equal(result.sources[0].point.received_at, '2026-09-07T01:00:01Z')
})

test('operator provenance uses exact declared source keys, never display-name guesses or cross-node L0', () => {
  const fixture = provenanceFixture()
  fixture.trunk.l0 = []
  assert.equal(runtimeModel.entityProvenanceModel(fixture).sources[0].point.tag_id, 'tag-power')
  fixture.l0[0].name = 'different'
  fixture.l0[0].display_name = 'ActivePower'
  assert.match(runtimeModel.entityProvenanceModel(fixture).sources[0].error, /L0.*ActivePower/)
  fixture.l0[0].name = 'ActivePower'
  fixture.l0[0].node_id = 'other-node'
  assert.match(runtimeModel.entityProvenanceModel(fixture).sources[0].error, /L0/)
})

test('cross-node L2 retains its source kind and key without borrowing a similarly named L0', () => {
  const fixture = provenanceFixture()
  fixture.trunk.l2[0].source_summary = [{ input_id: 'other-power', source_kind: 'l2', source_key: 'pcs.active_power' }]
  const result = runtimeModel.entityProvenanceModel(fixture)
  assert.equal(result.error, null)
  assert.deepEqual(result.sources, [{ kind: 'l2', inputId: 'other-power', sourceKey: 'pcs.active_power' }])
})

test('same-template rebinding cannot attribute the new source to an older committed observation', () => {
  const fixture = provenanceFixture()
  fixture.trunk.l1_summary.configuration_revision = 89
  fixture.trunk.l2[0].source_summary[0].source_key = 'ReboundPower'
  fixture.trunk.l0[0].source_key = 'ReboundPower'
  fixture.l0[0].name = 'ReboundPower'
  const result = runtimeModel.entityProvenanceModel(fixture)
  assert.match(result.error, /配置.*修订/)
  assert.deepEqual(result.sources, [])
  fixture.observation.configuration_revision = 89
  assert.equal(runtimeModel.entityProvenanceModel(fixture).sources[0].sourceKey, 'ReboundPower')
})

test('missing installed or observed configuration evidence fails closed even for the same template', () => {
  for (const revision of [undefined, null, 0, -1, NaN]) {
    const fixture = provenanceFixture()
    fixture.trunk.l1_summary.configuration_revision = revision
    assert.match(runtimeModel.entityProvenanceModel(fixture).error, /配置.*修订/)
  }
  const fixture = provenanceFixture()
  delete fixture.observation.configuration_revision
  assert.match(runtimeModel.entityProvenanceModel(fixture).error, /配置.*修订/)
})

test('missing trunk, identity, revision, output, processing and source mapping fail with concrete reasons', () => {
  for (const [mutate, reason] of [
    [(f) => { f.trunk = null }, /主干/],
    [(f) => { f.trunk.node_id = 'other' }, /节点/],
    [(f) => { f.observation = null }, /L2/],
    [(f) => { f.observation.entity_instance_id = 'other' }, /L2/],
    [(f) => { f.trunk.l1_summary.revision_id = 'new-revision' }, /修订/],
    [(f) => { f.trunk.l2 = [] }, /输出/],
    [(f) => { f.trunk.l2[0].processing_kind = null }, /加工/],
    [(f) => { f.trunk.l2[0].source_summary = [] }, /来源/],
  ]) {
    const fixture = provenanceFixture()
    mutate(fixture)
    assert.match(runtimeModel.entityProvenanceModel(fixture).error, reason)
  }
})

test('ambiguous and newer-than-L2 L0 evidence is unavailable rather than joined as proof', () => {
  const fixture = provenanceFixture()
  fixture.l0.push({ ...fixture.l0[0] })
  assert.match(runtimeModel.entityProvenanceModel(fixture).sources[0].error, /歧义/)
  fixture.l0.pop()
  fixture.l0[0].frame_sequence = 42
  assert.match(runtimeModel.entityProvenanceModel(fixture).sources[0].error, /帧/)
})

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

test('device category comes only from the explicit node type and unknown types stay in other', () => {
  assert.equal(typeof runtimeModel.deviceMonitorCategory, 'function')
  assert.deepEqual([
    ['PV', '光伏'],
    ['INVERTER', '光伏'],
    ['PCS', '储能'],
    ['BMS', '储能'],
    ['EVSE', '充电'],
    ['CHARGER', '充电'],
    ['METER', '电表'],
    ['GRID', '电表'],
    ['CUSTOM_DEVICE', '其他'],
    ['', '其他'],
  ].map(([nodeType]) => runtimeModel.deviceMonitorCategory(nodeType)), [
    '光伏', '光伏', '储能', '储能', '充电', '充电', '电表', '电表', '其他', '其他',
  ])
  assert.equal(runtimeModel.deviceMonitorCategory('名为PCS的自定义类型'), '其他')
})

test('device card metrics use exact definition ids for stable primary and secondary order', () => {
  assert.equal(typeof runtimeModel.orderDeviceMonitorEntities, 'function')
  const shuffled = [
    { ...descriptors[0], id: 'entity-z', definition_id: 'custom.power', display_name: 'PCS 有功功率（仅名称相似）' },
    { ...descriptors[0], id: 'entity-temp', definition_id: 'pcs.temp', display_name: '内部温度' },
    { ...descriptors[0], id: 'entity-status', definition_id: 'pcs.status', display_name: '运行状态' },
    { ...descriptors[0], id: 'entity-power', definition_id: 'pcs.activePower', display_name: '有功功率' },
    { ...descriptors[0], id: 'entity-limit', definition_id: 'pcs.dischargePowerLimit', display_name: '放电功率限值' },
  ]
  assert.deepEqual(
    runtimeModel.orderDeviceMonitorEntities('PCS', shuffled).map((entity) => entity.id),
    ['entity-power', 'entity-limit', 'entity-temp', 'entity-status', 'entity-z'],
  )
  assert.deepEqual(
    runtimeModel.orderDeviceMonitorEntities('UNLISTED', shuffled).map((entity) => entity.id),
    ['entity-z', 'entity-power', 'entity-limit', 'entity-status', 'entity-temp'],
  )
})

test('device quality summary distinguishes current, last evidence, unconfigured and unknown', () => {
  assert.equal(typeof runtimeModel.deviceMonitorDataState, 'function')
  assert.equal(typeof runtimeModel.summarizeDeviceMonitorDataStates, 'function')
  const observation = completeFrame('node-pcs-a', descriptors[0]).l2[0]
  const current = [{ descriptor: descriptors[0], observation }]
  const last = [{ descriptor: descriptors[0], observation: { ...observation, quality: 64, reason: 'ENTITY_DATA_STALE' } }]
  const missingObservation = [{ descriptor: descriptors[0], observation: null }]

  assert.equal(runtimeModel.deviceMonitorDataState(current, true), 'current')
  assert.equal(runtimeModel.deviceMonitorDataState(current, false), 'last')
  assert.equal(runtimeModel.deviceMonitorDataState(last, true), 'last')
  assert.equal(runtimeModel.deviceMonitorDataState([], true), 'unconfigured')
  assert.equal(runtimeModel.deviceMonitorDataState(null, true), 'unknown')
  assert.equal(runtimeModel.deviceMonitorDataState(missingObservation, true), 'unknown')
  assert.deepEqual(
    runtimeModel.summarizeDeviceMonitorDataStates(['current', 'last', 'unconfigured', 'unknown', 'last']),
    { current: 1, last: 2, unconfigured: 1, unknown: 1 },
  )
})

test('mixed-quality device cards retain the exact last-value time with observed-time fallback', () => {
  assert.equal(typeof runtimeModel.deviceMonitorEvidenceTime, 'function')
  const observation = completeFrame('node-pcs-a', descriptors[0]).l2[0]
  const current = runtimeEntityReading(observation, true)
  const stale = runtimeEntityReading({
    ...observation,
    quality: 64,
    reason: 'ENTITY_DATA_STALE',
    observed_at: '2026-09-07T01:02:03.000Z',
    value_observed_at: '2026-09-07T01:01:57.000Z',
  }, true)
  const badWithoutValueTime = runtimeEntityReading({
    ...observation,
    quality: 0,
    reason: 'INPUT_BAD',
    observed_at: '2026-09-07T01:02:01.000Z',
    value_observed_at: null,
  }, true)

  assert.equal(runtimeModel.deviceMonitorDataState([
    { descriptor: descriptors[0], observation },
    { descriptor: { ...descriptors[0], id: 'stale' }, observation: { ...observation, entity_instance_id: 'stale', quality: 64 } },
    { descriptor: { ...descriptors[0], id: 'bad' }, observation: { ...observation, entity_instance_id: 'bad', quality: 0 } },
  ], true), 'last')
  assert.equal(runtimeModel.deviceMonitorEvidenceTime(current), null)
  assert.equal(runtimeModel.deviceMonitorEvidenceTime(stale), '2026-09-07T01:01:57.000Z')
  assert.equal(runtimeModel.deviceMonitorEvidenceTime(badWithoutValueTime), '2026-09-07T01:02:01.000Z')
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
  assert.equal(first.pageItems[0].category, '储能')
  assert.equal(second.pageItems[1].category, '其他')

  const byName = buildDeviceMonitorPage({ nodes, descriptors: [entity], alarmCounts: {}, query: 'DEVICE-2', category: '', onlyAlarms: false, page: 1 })
  const byType = buildDeviceMonitorPage({ nodes, descriptors: [entity], alarmCounts: {}, query: '', category: '储能', onlyAlarms: false, page: 1 })
  assert.deepEqual(byName.activeNodeIds, ['device-2'])
  assert.deepEqual(byType.activeNodeIds, ['device-1', 'device-2', 'device-3', 'device-4', 'device-5', 'device-6'])
  assert.deepEqual(byType.pageItems[0].entities.map((item) => item.id), [])
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
