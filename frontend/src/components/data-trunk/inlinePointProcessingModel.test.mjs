import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
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

test('Chinese raw point names receive distinct valid entity identities', async () => {
  const model = await import('./inlinePointProcessingModel.ts')
  const first = model.suggestInlinePointProcessingDefaults([{
    ...POWER,
    id: '7d72a1c5-1111-4444-8888-111111111111',
    name: '交流总有功功率',
  }], 'PCS')
  const second = model.suggestInlinePointProcessingDefaults([{
    ...CURRENT,
    id: '93fe2d60-2222-4444-8888-222222222222',
    name: '交流总无功功率',
  }], 'PCS')

  assert.match(first.definitionKey, /^pcs\.[a-z][a-z0-9_]*$/)
  assert.match(second.definitionKey, /^pcs\.[a-z][a-z0-9_]*$/)
  assert.notEqual(first.definitionKey, second.definitionKey)
  assert.equal(first.displayName, '交流总有功功率')
})

test('technical identity and freshness inputs stay inside advanced settings', async () => {
  const source = await readFile(new URL('./InlinePointProcessingPanel.tsx', import.meta.url), 'utf8')
  const advanced = source.indexOf('高级设置')

  assert.ok(advanced >= 0)
  assert.ok(source.indexOf('业务标识') > advanced)
  assert.ok(source.indexOf('超时秒数') > advanced)
})

test('committed-frame trial projects value quality time and source evidence', async () => {
  const model = await import('./inlinePointProcessingModel.ts')

  const result = model.projectInlinePointProcessingTrial({
    available: true,
    frame_sequence: 12,
    frame_time: '2026-08-29T10:00:00+00:00',
    configuration_revision: 7,
    outputs: [{
      entity_instance_id: 'entity-1',
      entity_definition_id: 'pcs.combined_power',
      value: 42,
      data_type: 'FLOAT',
      unit: 'W',
      quality: 192,
      reason: null,
      observed_at: '2026-08-29T09:59:59+00:00',
      source_ids: ['observation-1', 'observation-2'],
    }],
  })

  assert.deepEqual(result, {
    status: 'available',
    valueText: '42 W',
    qualityText: '正常',
    evidenceText: '2 个来源 · 帧 12 · 配置 7',
    observedAt: '2026-08-29T09:59:59+00:00',
    message: '',
  })
})

test('unavailable trial is explicit in Chinese and never looks like zero', async () => {
  const model = await import('./inlinePointProcessingModel.ts')

  const result = model.projectInlinePointProcessingTrial({
    available: false,
    reason: 'POINT_PROCESSING_TRIAL_FRAME_UNAVAILABLE',
    message: 'No committed frame is available',
  })

  assert.deepEqual(result, {
    status: 'unavailable',
    valueText: '—',
    qualityText: '未试算',
    evidenceText: '',
    observedAt: null,
    message: '当前还没有可用于试算的已提交数据帧。',
  })
})
