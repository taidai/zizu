import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildTwoChargeTwoDischargeJdm,
  describeDispatchStrategyError,
  makeStrategyBinding,
  normalizeDispatchWindows,
  projectStrategyStatus,
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
