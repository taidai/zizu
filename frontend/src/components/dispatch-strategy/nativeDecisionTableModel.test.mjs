import assert from 'node:assert/strict'
import test from 'node:test'

const model = await import('./nativeDecisionTableModel.ts')
const schedule = await import('./dispatchStrategyModel.mjs')

const singleTableGraph = () => ({
  nodes: [
    { id: 'input', type: 'inputNode', content: { untouched: true } },
    { id: 'rules', type: 'decisionTableNode', name: '峰谷规则', content: {
      hitPolicy: 'first',
      inputs: [{ id: 'temperature', field: 'temperature', metadata: { source: 'plant-a' } }],
      outputs: [{ id: 'fan', field: 'fan', vendorFlag: true }],
      rules: [],
      metadata: { owner: 'plant-a' },
    }, custom: 'keep' },
    { id: 'unknown', type: 'vendorNode', content: { formula: 'x + 1' } },
  ],
  edges: [{ id: 'edge-1', sourceId: 'input', targetId: 'rules', metadata: { keep: true } }],
  metadata: { owner: 'site-a', nested: { revision: 7 } },
})

test('generic strategy starter is a native table without fixed SOC schedule semantics', () => {
  const graph = model.buildGenericDecisionTableJdm()
  assert.deepEqual(model.inspectNativeDecisionTable(graph), {
    nodeId: 'decision-table',
    content: {
      hitPolicy: 'collect',
      outputPath: 'intents',
      inputs: [],
      outputs: [
        { id: 'action_id', field: 'action_id', name: '输出别名（action_id）' },
        { id: 'target', field: 'target', name: '目标值（target）' },
      ],
      rules: [],
    },
  })
  assert.equal(JSON.stringify(graph).includes('soc'), false)
  assert.equal(JSON.stringify(graph).includes('power-target'), false)
})

test('stored legacy schedule cells are not silently converted to the corrected template', () => {
  const graph = schedule.buildTwoChargeTwoDischargeJdm([
    { key: 'discharge', start: '10:00', end: '12:00', action: 'DISCHARGE', target: 20, socMin: 40, socMax: 90 },
  ], 0)
  const content = graph.nodes[1].content
  content.rules[0].site_local_minute = 'site_local_minute >= 600 && site_local_minute < 720'
  content.rules[0].soc = 'soc >= 40 && soc <= 90'
  const before = structuredClone(graph)
  assert.equal(schedule.readTwoChargeTwoDischargeJdm(graph), null)
  assert.equal(model.inspectNativeDecisionTable(graph).nodeId, 'schedule')
  assert.deepEqual(model.replaceDecisionTableContent(graph, 'schedule', content), before)
  assert.deepEqual(graph, before)
})

test('a unique decision table is inspected without changing the graph', () => {
  const graph = singleTableGraph()
  const before = structuredClone(graph)
  assert.deepEqual(model.inspectNativeDecisionTable(graph), { nodeId: 'rules', content: graph.nodes[1].content })
  assert.deepEqual(graph, before)
})

test('zero or multiple decision tables stay in the full graph editor', () => {
  assert.equal(model.inspectNativeDecisionTable({ nodes: [], edges: [] }), null)
  const graph = singleTableGraph()
  graph.nodes.push({ id: 'rules-2', type: 'decisionTableNode', content: { rules: [] } })
  assert.equal(model.inspectNativeDecisionTable(graph), null)
  assert.throws(() => model.replaceDecisionTableContent(graph, 'rules', { rules: [] }), /完整规则图/)
  assert.equal(model.inspectNativeDecisionTable({ nodes: [{ id: 'broken', type: 'decisionTableNode', content: { rules: [] } }] }), null)
})

test('native edit replaces only the sole table content and preserves every unknown field', () => {
  const graph = singleTableGraph()
  const before = structuredClone(graph)
  const content = {
    hitPolicy: 'collect',
    inputs: [{ id: 'temperature', field: 'ambient_temperature' }],
    outputs: [{ id: 'fan', field: 'fan_enable' }],
    rules: [{ _id: 'changed', temperature: 'temperature > 30' }],
  }
  const next = model.replaceDecisionTableContent(graph, 'rules', content)
  assert.notEqual(next, graph)
  assert.notEqual(next.nodes, graph.nodes)
  assert.deepEqual(graph, before)
  assert.deepEqual(next.metadata, before.metadata)
  assert.deepEqual(next.edges, before.edges)
  assert.deepEqual(next.nodes[0], before.nodes[0])
  assert.deepEqual(next.nodes[2], before.nodes[2])
  assert.deepEqual(next.nodes[1], { ...before.nodes[1], content: {
    ...content,
    inputs: [{ ...content.inputs[0], metadata: { source: 'plant-a' } }],
    outputs: [{ ...content.outputs[0], vendorFlag: true }],
    metadata: { owner: 'plant-a' },
  } })
})

test('a newly added column does not inherit unknown fields from a removed column at the same position', () => {
  const graph = singleTableGraph()
  const next = model.replaceDecisionTableContent(graph, 'rules', {
    hitPolicy: 'first', inputs: [{ id: 'humidity', field: 'humidity' }], outputs: [], rules: [],
  })
  assert.deepEqual(next.nodes[1].content.inputs, [{ id: 'humidity', field: 'humidity' }])
})

test('native row edits preserve unknown evidence while removing intentionally deleted column cells', () => {
  const graph = singleTableGraph()
  graph.nodes[1].content.rules = [{ _id: 'row-1', temperature: '> 30', fan: 'true', vendorEvidence: { revision: 9 } }]
  const next = model.replaceDecisionTableContent(graph, 'rules', {
    ...graph.nodes[1].content, outputs: [], rules: [{ _id: 'row-1', temperature: '> 35' }],
  })
  assert.deepEqual(next.nodes[1].content.rules, [{ _id: 'row-1', temperature: '> 35', vendorEvidence: { revision: 9 } }])
})

test('optional examples add only native input columns without replacing existing rules or graph metadata', () => {
  assert.equal(typeof model.addNativeExampleColumns, 'function')
  const graph = singleTableGraph()
  const next = model.addNativeExampleColumns(graph, 'rules')
  assert.deepEqual(next.nodes[1].content.inputs.map((column) => column.field), ['temperature', 'site_local_minute', 'soc'])
  assert.deepEqual(next.nodes[1].content.rules, [])
  assert.deepEqual(next.metadata, graph.metadata)
  assert.deepEqual(model.addNativeExampleColumns(next, 'rules'), next)
})

test('edited bindings remain the save source after a native graph becomes a multi-table graph', () => {
  const current = [{ direction: 'OUTPUT', binding_key: 'fan', ordinal: 0, entity_instance_id: 'fan-A', expected_data_type: 'BOOL', unit: null, freshness_seconds: 5 }]
  const edited = [{ ...current[0], entity_instance_id: 'fan-B' }]
  const saved = model.bindingsForDraft(edited)
  assert.deepEqual(saved, edited)
  assert.notEqual(saved, edited)
  assert.notDeepEqual(saved, current)
})

test('general L2 input accepts confirmed readable bool, numeric, and string entities', () => {
  const base = { confirmed: true, direction: 'R' }
  for (const data_type of ['BOOL', 'BOOLEAN', 'INT', 'FLOAT', 'NUMBER', 'STRING', 'STATE', 'ENUM']) {
    assert.equal(model.isNativeDecisionInputEntity({ ...base, data_type }), true, data_type)
  }
  assert.equal(model.isNativeDecisionInputEntity({ ...base, data_type: 'BINARY' }), false)
  assert.equal(model.isNativeDecisionInputEntity({ ...base, data_type: 'FLOAT', confirmed: false }), false)
  assert.equal(model.isNativeDecisionInputEntity({ ...base, data_type: 'FLOAT', direction: 'W' }), false)
})

test('general output requires an explicitly controllable confirmed writable L2 entity', () => {
  const output = { confirmed: true, direction: 'RW', data_type: 'FLOAT', control_eligible: true }
  assert.equal(model.isNativeDecisionOutputEntity(output), true)
  assert.equal(model.isNativeDecisionOutputEntity({ ...output, direction: 'W', data_type: 'BOOL' }), true)
  for (const patch of [{ confirmed: false }, { direction: 'R' }, { control_eligible: false }, { control_eligible: undefined }, { data_type: 'BINARY' }]) {
    assert.equal(model.isNativeDecisionOutputEntity({ ...output, ...patch }), false)
  }
})

test('binding aliases are generic and unique', () => {
  assert.deepEqual(model.validateBindingAliases([
    { direction: 'INPUT', binding_key: 'room_temp' },
    { direction: 'INPUT', binding_key: 'tariff_state' },
    { direction: 'OUTPUT', binding_key: 'fan_enable' },
  ]), { valid: true, message: '' })
  assert.match(model.validateBindingAliases([
    { direction: 'INPUT', binding_key: 'room_temp' },
    { direction: 'INPUT', binding_key: 'room_temp' },
  ]).message, /唯一/)
  assert.equal(model.validateBindingAliases([
    { direction: 'INPUT', binding_key: 'state' },
    { direction: 'OUTPUT', binding_key: 'state' },
  ]).valid, true)
  assert.match(model.validateBindingAliases([{ direction: 'INPUT', binding_key: '   ' }]).message, /不能为空/)
})

test('editing one binding preserves order, extra bindings, and untouched server fields', () => {
  const bindings = [
    { direction: 'INPUT', binding_key: 'temperature', ordinal: 0, entity_instance_id: 'old', expected_data_type: 'FLOAT', unit: 'C', freshness_seconds: 10, server_extension: { keep: true } },
    { direction: 'INPUT', binding_key: 'tariff', ordinal: 1, entity_instance_id: 'tariff-1', expected_data_type: 'STRING', unit: null, freshness_seconds: 30, extra: 'keep' },
    { direction: 'OUTPUT', binding_key: 'fan', ordinal: 0, entity_instance_id: 'fan-1', expected_data_type: 'BOOL', unit: null, freshness_seconds: 5 },
  ]
  const before = structuredClone(bindings)
  const next = model.updateStrategyBinding(bindings, 0, { id: 'temperature-2', data_type: 'NUMBER', unit: 'degC', freshness_seconds: 15 }, 'room_temp')
  assert.deepEqual(bindings, before)
  assert.deepEqual(next[0], { ...before[0], binding_key: 'room_temp', entity_instance_id: 'temperature-2', expected_data_type: 'NUMBER', unit: 'degC', freshness_seconds: 15 })
  assert.deepEqual(next.slice(1), before.slice(1))
})
