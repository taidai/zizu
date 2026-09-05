import assert from 'node:assert/strict'
import test from 'node:test'

const source = {
  schemaVersion: 'zizu.point-processing/v1alpha1',
  id: 'pcs.en9',
  kind: 'point_processing_template',
  displayName: 'EN9 PCS',
  deviceCategory: 'PCS',
  brand: 'EN9',
  model: 'EN9-PCS',
  revision: 2,
  status: 'active',
  inputs: [{ id: 'power', sourceKind: 'l0', sourceKey: 'ActivePower', aliases: [], dataType: 'FLOAT', unit: 'kW', required: true }],
  outputs: [{ id: 'active_power', entityDefinition: 'pcs.active_power', dataType: 'FLOAT', unit: 'kW', freshness: '30s', transform: { kind: 'numeric', input: 'power', scale: 1, offset: 0 } }],
}

test('copying a published template always creates an immutable next revision', async () => {
  const model = await import('./pointProcessingTemplateEditorModel.ts')

  const next = model.cloneTemplateDraft(source, 'next-revision')
  const separate = model.cloneTemplateDraft(source, 'new-template')

  assert.equal(next.id, source.id)
  assert.equal(next.revision, 3)
  assert.equal(separate.revision, 1)
  assert.equal(separate.id, 'pcs.en9.copy')
  assert.notEqual(next.inputs, source.inputs)
})

test('four visual processing kinds produce the existing canonical JSON shape', async () => {
  const model = await import('./pointProcessingTemplateEditorModel.ts')

  assert.deepEqual(model.buildTransform('passthrough', 'power'), {
    kind: 'numeric', input: 'power', scale: 1, offset: 0, minimum: -1000000000, maximum: 1000000000,
  })
  assert.deepEqual(model.buildTransform('numeric', 'power', { scale: '0.001', offset: '-2', minimum: '-500', maximum: '500' }), {
    kind: 'numeric', input: 'power', scale: 0.001, offset: -2, minimum: -500, maximum: 500,
  })
  assert.deepEqual(model.buildTransform('enum', 'power', { entries: '0=STOPPED\n1=RUNNING' }), {
    kind: 'enum', input: 'power', entries: { 0: 'STOPPED', 1: 'RUNNING' },
  })
  assert.deepEqual(model.buildTransform('formula', 'power', { expression: 'power' }), {
    kind: 'formula', expression: 'power', scheduleSeconds: 1, controlEligible: false,
  })
  assert.throws(
    () => model.buildTransform('formula', 'power', { expression: '  ' }),
    /公式不能为空/,
  )
})

test('unknown template fields survive draft cloning', async () => {
  const model = await import('./pointProcessingTemplateEditorModel.ts')
  const extended = { ...source, deliveryNote: 'keep-me' }

  const draft = model.cloneTemplateDraft(extended, 'next-revision')

  assert.equal(draft.deliveryNote, 'keep-me')
})

test('editing current node processing keeps its complete immutable source as an isolated draft', async () => {
  const model = await import('./pointProcessingTemplateEditorModel.ts')
  const extended = { ...source, ownerEvidence: { node: 'pcs-1' } }

  const draft = model.createNodeProcessingEditDraft(extended)

  assert.deepEqual(draft, extended)
  assert.notEqual(draft, extended)
  assert.notEqual(draft.inputs, extended.inputs)
  draft.displayName = 'edited'
  assert.equal(extended.displayName, 'EN9 PCS')
})

test('current node edit can fill only missing numeric local-L0 passthrough output units', async () => {
  const model = await import('./pointProcessingTemplateEditorModel.ts')
  const document = {
    ...source,
    inputs: [
      { id: 'raw_real', sourceKind: 'l0', sourceKey: 'PReal', aliases: [], dataType: 'FLOAT', unit: null, required: true },
      { id: 'raw_legacy', sourceKind: 'l0', sourceKey: 'PLegacy', aliases: [], dataType: 'INT', unit: null, required: true },
      { id: 'known', sourceKind: 'l0', sourceKey: 'PKnown', aliases: [], dataType: 'FLOAT', unit: 'W', required: true },
      { id: 'flag', sourceKind: 'l0', sourceKey: 'Flag', aliases: [], dataType: 'BOOL', unit: null, required: true },
      { id: 'remote', sourceKind: 'l2', sourceKey: 'site.power', aliases: [], dataType: 'FLOAT', unit: null, required: true },
    ],
    outputs: [
      { id: 'real', entityDefinition: 'pcs.real', dataType: 'FLOAT', unit: null, freshness: '30s', transform: { kind: 'passthrough', input: 'raw_real' }, control: { minimum: 0, maximum: 200 } },
      { id: 'legacy', entityDefinition: 'pcs.legacy', dataType: 'INT', unit: null, freshness: '30s', transform: { kind: 'numeric', input: 'raw_legacy', scale: 1, offset: 0, minimum: -1000000000, maximum: 1000000000 } },
      { id: 'already_known', entityDefinition: 'pcs.known', dataType: 'FLOAT', unit: 'W', freshness: '30s', transform: { kind: 'passthrough', input: 'known' } },
      { id: 'known_input', entityDefinition: 'pcs.known_input', dataType: 'FLOAT', unit: null, freshness: '30s', transform: { kind: 'passthrough', input: 'known' } },
      { id: 'flag_output', entityDefinition: 'pcs.flag', dataType: 'BOOL', unit: null, freshness: '30s', transform: { kind: 'passthrough', input: 'flag' } },
      { id: 'remote_output', entityDefinition: 'pcs.remote', dataType: 'FLOAT', unit: null, freshness: '30s', transform: { kind: 'passthrough', input: 'remote' } },
      { id: 'scaled', entityDefinition: 'pcs.scaled', dataType: 'FLOAT', unit: null, freshness: '30s', transform: { kind: 'numeric', input: 'raw_real', scale: 0.001, offset: 0, minimum: -1000000000, maximum: 1000000000 } },
    ],
  }

  assert.deepEqual(model.missingUnitDeclarationOutputIds(document), ['real', 'legacy'])
})

test('real passthrough remains a real passthrough while declaring a missing unit', async () => {
  const model = await import('./pointProcessingTemplateEditorModel.ts')
  const document = {
    ...source,
    inputs: [{ id: 'power', sourceKind: 'l0', sourceKey: 'ActivePower', aliases: [], dataType: 'FLOAT', unit: null, required: true }],
    outputs: [{
      id: 'active_power',
      entityDefinition: 'pcs.active_power',
      dataType: 'FLOAT',
      unit: null,
      freshness: '30s',
      transform: { kind: 'passthrough', input: 'power' },
      control: { minimum: 0, maximum: 200, tolerance: 0.1 },
    }],
  }

  const draft = model.updateNodeProcessingOutputUnit(document, 'active_power', 'kW')

  assert.equal(document.outputs[0].unit, null)
  assert.equal(draft.outputs[0].unit, 'kW')
  assert.deepEqual(draft.outputs[0].transform, { kind: 'passthrough', input: 'power' })
  assert.deepEqual(draft.outputs[0].control, { minimum: 0, maximum: 200, tolerance: 0.1 })
})

test('a bounded numeric rule is not mislabeled as pass-through', async () => {
  const model = await import('./pointProcessingTemplateEditorModel.ts')

  assert.equal(model.visualTransformKind({
    kind: 'numeric', input: 'power', scale: 1, offset: 0, minimum: -500, maximum: 500,
  }), 'numeric')
})

test('current node L0 points become a typed editable template draft', async () => {
  const model = await import('./pointProcessingTemplateEditorModel.ts')

  const draft = model.createTemplateDraftFromL0([
    { source_key: 'ActivePower', data_type: 'FLOAT', unit: 'kW' },
    { source_key: '运行状态', data_type: 'INT', unit: null },
  ], {
    id: 'pcs.custom',
    displayName: '现场 PCS 模板',
    deviceCategory: 'PCS',
    brand: '现场品牌',
    model: '现场型号',
  })

  assert.equal(draft.revision, 1)
  assert.deepEqual(draft.inputs.map((input) => [input.id, input.sourceKey, input.dataType, input.required]), [
    ['activepower', 'ActivePower', 'FLOAT', true],
    ['point_2', '运行状态', 'INT', true],
  ])
  assert.deepEqual(draft.outputs.map((output) => [output.id, output.entityDefinition, output.dataType, output.transform.kind]), [
    ['activepower', 'pcs.activepower', 'FLOAT', 'numeric'],
    ['point_2', 'pcs.point_2', 'INT', 'formula'],
  ])
  assert.equal(draft.outputs[1].transform.expression, 'point_2')
})
