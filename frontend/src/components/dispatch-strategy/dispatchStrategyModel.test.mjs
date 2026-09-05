import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildTwoChargeTwoDischargeJdm,
  describeDispatchStrategyError,
  isDispatchSocEntity,
  isDispatchPowerTargetEntity,
  makeStrategyBinding,
  normalizeDispatchWindows,
  projectStrategyStatus,
  readTwoChargeTwoDischargeJdm,
  splitCrossMidnight,
  validateDispatchWindows,
} from './dispatchStrategyModel.mjs'

const row = (key, start, end, target = 0) => ({
  key,
  start,
  end,
  action: target < 0 ? 'CHARGE' : target > 0 ? 'DISCHARGE' : 'HOLD',
  target,
  socMin: 10,
  socMax: 90,
})

test('local-time windows sort deterministically without mutating input', () => {
  const input = [row('later', '18:00', '22:00', 80), row('early', '00:00', '06:00', -60)]
  const before = structuredClone(input)
  assert.deepEqual(normalizeDispatchWindows(input).map((item) => item.key), ['early', 'later'])
  assert.deepEqual(input, before)
})

test('overlaps are highlighted before submit', () => {
  const result = validateDispatchWindows([
    row('charge-1', '00:00', '06:00', -60),
    row('charge-2', '05:30', '08:00', -40),
  ], 0)
  assert.equal(result.valid, false)
  assert.deepEqual(result.overlapKeys, ['charge-1', 'charge-2'])
  assert.match(result.message, /时间重叠/)
})

test('cross-midnight time range splits into explicit day rows', () => {
  assert.deepEqual(splitCrossMidnight(row('night', '22:00', '02:00', -30)), [
    { ...row('night', '22:00', '02:00', -30), key: 'night:late', end: '24:00' },
    { ...row('night', '22:00', '02:00', -30), key: 'night:early', start: '00:00' },
  ])
})

test('other-time safe target is required', () => {
  const result = validateDispatchWindows([row('day', '08:00', '10:00', 50)], '')
  assert.equal(result.valid, false)
  assert.match(result.message, /其他时段安全目标/)
})

test('typed L2 binding preserves instance identity, type, unit and freshness', () => {
  const entity = {
    id: 'entity-1',
    data_type: 'FLOAT',
    unit: 'kW',
    freshness_seconds: 15,
  }
  assert.deepEqual(makeStrategyBinding(entity, 'OUTPUT', 'power-target', 0), {
    direction: 'OUTPUT',
    binding_key: 'power-target',
    ordinal: 0,
    entity_instance_id: 'entity-1',
    expected_data_type: 'FLOAT',
    unit: 'kW',
    freshness_seconds: 15,
  })
})

test('API validation error maps to one plain-Chinese field message', () => {
  assert.equal(
    describeDispatchStrategyError({ detail: { code: 'OUTPUT_LIMIT_VIOLATION' } }),
    '功率目标超出该实体允许的控制范围。',
  )
  assert.equal(
    describeDispatchStrategyError({ detail: [{ loc: ['body', 'name'], msg: 'Field required' }] }),
    '请检查“名称”后重试。',
  )
})

test('strategy status keeps revision, enable state and runtime health separate', () => {
  assert.deepEqual(projectStrategyStatus({
    draft: { revision: 3, lifecycle: 'DRAFT' },
    published_revision: { revision: 2, lifecycle: 'PUBLISHED' },
    active_revision: { revision: 2, lifecycle: 'PUBLISHED' },
    enabled: true,
    runtime_health: 'BLOCKED',
    failure_code: 'L2_INPUT_STALE',
  }), {
    draftRevision: 3,
    publishedRevision: 2,
    lifecycleLabel: '有未发布修改',
    enableLabel: '已启用',
    healthLabel: '阻断',
    healthDetail: 'L2_INPUT_STALE',
  })
})

test('a published strategy is published before it is enabled', () => {
  assert.deepEqual(projectStrategyStatus({
    draft: null,
    published_revision: { id: 'published-2', revision: 2 },
    active_revision: null,
    enabled: false,
    runtime_health: 'READY',
  }), {
    draftRevision: null,
    publishedRevision: 2,
    lifecycleLabel: '已发布',
    enableLabel: '已停用',
    healthLabel: '就绪',
    healthDetail: '',
  })
})

test('the latest published revision is not confused with the older running revision', () => {
  const status = projectStrategyStatus({
    draft: null,
    published_revision: { id: 'published-3', revision: 3 },
    active_revision: { id: 'published-2', revision: 2 },
    enabled: true,
    runtime_health: 'READY',
  })
  assert.equal(status.publishedRevision, 3)
  assert.equal(status.lifecycleLabel, '已发布')
  assert.equal(status.enableLabel, '已启用')
})

test('easy table compiles to the sole standard JDM document', () => {
  const graph = buildTwoChargeTwoDischargeJdm([
    row('charge-1', '00:00', '06:00', -60),
    row('discharge-1', '10:00', '12:00', 80),
  ], 0)
  const table = graph.nodes.find((node) => node.type === 'decisionTableNode').content
  assert.equal(table.hitPolicy, 'first')
  assert.equal(table.rules.at(-1)._id, 'other-time')
  assert.equal(table.rules.at(-1).target, '0')
  assert.deepEqual(graph.edges.map((edge) => edge.type), ['edge', 'edge'])
})

test('only an exactly representable JDM exposes editable schedule cells', () => {
  const windows = [row('day', '08:00', '24:00', 50)]
  const graph = buildTwoChargeTwoDischargeJdm(windows, 0)
  // PostgreSQL JSON object key order is not part of the JDM contract.
  const reordered = { edges: graph.edges, nodes: graph.nodes }
  assert.deepEqual(readTwoChargeTwoDischargeJdm(reordered), { rows: windows, safeTarget: 0 })
})

test('unrepresented JDM structure never exposes editable schedule cells', () => {
  const mutations = [
    (graph) => { graph.nodes.push({ id: 'extra', type: 'expressionNode', content: {} }) },
    (graph) => { graph.edges[0].targetId = 'output' },
    (graph) => { graph.nodes[1].content.hitPolicy = 'collect' },
    (graph) => { graph.nodes[1].content.rules[0].soc += ' || override' },
    (graph) => { graph.nodes[1].content.rules[0].action_id = '"other-target"' },
    (graph) => { graph.nodes[1].content.rules[0].temperature = 'temperature < 40' },
    (graph) => { graph.nodes[1].content.rules.at(-1).soc = 'soc > 20' },
    (graph) => { graph.nodes[1].content.rules.reverse() },
    (graph) => { graph.metadata = { custom: true } },
  ]
  for (const mutate of mutations) {
    const graph = buildTwoChargeTwoDischargeJdm([row('day', '08:00', '10:00', 50)], 0)
    mutate(graph)
    const original = structuredClone(graph)
    assert.equal(readTwoChargeTwoDischargeJdm(graph), null)
    assert.deepEqual(graph, original)
  }
  assert.equal(readTwoChargeTwoDischargeJdm({ nodes: [], edges: [] }), null)
})

test('SOC candidates require confirmed readable standard numeric percentage entities', () => {
  const entity = { confirmed: true, direction: 'R', definition_id: 'bms.soc', data_type: 'FLOAT', unit: '%' }
  assert.equal(isDispatchSocEntity(entity), true)
  assert.equal(isDispatchSocEntity({ ...entity, definition_id: 'storage.soc', data_type: 'INT', direction: 'RW' }), true)
  for (const patch of [
    { confirmed: false }, { direction: 'W' }, { definition_id: 'ess.soc' },
    { definition_id: 'bms.current', display_name: 'SOC' }, { data_type: 'BOOL' },
    { unit: 'ratio' }, { unit: null },
  ]) assert.equal(isDispatchSocEntity({ ...entity, ...patch }), false)
  assert.equal(isDispatchSocEntity(null), false)
})

test('power-target candidates require confirmed writable numeric kW entities', () => {
  const entity = { confirmed: true, direction: 'RW', data_type: 'FLOAT', unit: 'kW' }
  assert.equal(isDispatchPowerTargetEntity(entity), true)
  assert.equal(isDispatchPowerTargetEntity({ ...entity, direction: 'W', data_type: 'INT' }), true)
  for (const patch of [{ confirmed: false }, { direction: 'R' }, { data_type: 'BOOL' }, { unit: 'W' }, { unit: 'V' }]) {
    assert.equal(isDispatchPowerTargetEntity({ ...entity, ...patch }), false)
  }
  assert.equal(isDispatchPowerTargetEntity(null), false)
})

test('SOC and power contract errors explain the field that must be corrected', () => {
  for (const code of ['SOC_BINDING_DEFINITION_INVALID', 'SOC_BINDING_TYPE_INVALID', 'SOC_BINDING_UNIT_INVALID', 'SOC_VALUE_INVALID']) {
    assert.match(describeDispatchStrategyError({ code }), /SOC/)
  }
  for (const code of ['POWER_TARGET_BINDING_TYPE_INVALID', 'POWER_TARGET_BINDING_UNIT_INVALID', 'POWER_TARGET_VALUE_INVALID']) {
    assert.match(describeDispatchStrategyError({ code }), /功率/)
  }
})
