import assert from 'node:assert/strict'
import test from 'node:test'

const POWER = {
  id: 'tag-power',
  name: 'ActivePowerRaw',
  display_name: '交流总有功功率',
  data_type: 'FLOAT',
  unit: 'W',
}

const CURRENT = {
  id: 'tag-current',
  name: 'CurrentRaw',
  display_name: '交流电流',
  data_type: 'FLOAT',
  unit: 'A',
}

test('one selected L0 becomes one direct L2 draft', async () => {
  const model = await import('./inlinePointProcessingModel.ts')

  const result = model.buildNodePointProcessingDraft([POWER], {
    mode: 'passthrough',
    definitionKey: 'pcs.active_power',
    displayName: 'PCS 有功功率',
    deviceCategory: 'PCS',
    unit: 'W',
    freshnessSeconds: 10,
  })

  assert.equal(result.content.inputs.length, 1)
  assert.equal(result.content.outputs[0].entityDefinition, 'pcs.active_power')
  assert.deepEqual(result.inputSelections, { activepowerraw: 'tag-power' })
  assert.equal(result.content.outputs[0].transform.kind, 'numeric')
})

test('formula draft binds every selected L0 and never mutates points', async () => {
  const model = await import('./inlinePointProcessingModel.ts')
  const points = [POWER, CURRENT]
  const before = structuredClone(points)

  const result = model.buildNodePointProcessingDraft(points, {
    mode: 'formula',
    definitionKey: 'pcs.power_per_amp',
    displayName: '单位电流功率',
    deviceCategory: 'PCS',
    dataType: 'FLOAT',
    unit: 'W/A',
    freshnessSeconds: 10,
    expression: 'activepowerraw / currentraw',
  })

  assert.deepEqual(points, before)
  assert.deepEqual(result.inputSelections, {
    activepowerraw: 'tag-power',
    currentraw: 'tag-current',
  })
  assert.equal(result.content.outputs[0].transform.kind, 'formula')
  assert.equal(result.content.outputs[0].transform.expression, 'activepowerraw / currentraw')
})

test('simple processing rejects an empty selection and multi-point direct mapping', async () => {
  const model = await import('./inlinePointProcessingModel.ts')
  const form = {
    mode: 'passthrough',
    definitionKey: 'pcs.active_power',
    displayName: 'PCS 有功功率',
    deviceCategory: 'PCS',
    unit: 'W',
    freshnessSeconds: 10,
  }

  assert.throws(() => model.buildNodePointProcessingDraft([], form), /至少选择一个原始点位/)
  assert.throws(() => model.buildNodePointProcessingDraft([POWER, CURRENT], form), /多点加工请选择公式/)
})

test('direct mapping cannot relabel units and numeric conversion always outputs a number', async () => {
  const model = await import('./inlinePointProcessingModel.ts')
  const base = {
    definitionKey: 'pcs.active_power',
    displayName: 'PCS 有功功率',
    deviceCategory: 'PCS',
    freshnessSeconds: 10,
  }

  assert.throws(() => model.buildNodePointProcessingDraft([POWER], {
    ...base,
    mode: 'passthrough',
    unit: 'kW',
  }), /直接使用不能改变单位/)

  const converted = model.buildNodePointProcessingDraft([{ ...POWER, data_type: 'INT' }], {
    ...base,
    mode: 'numeric',
    unit: 'kW',
    scale: 0.001,
  })
  assert.equal(converted.content.outputs[0].dataType, 'FLOAT')
})
