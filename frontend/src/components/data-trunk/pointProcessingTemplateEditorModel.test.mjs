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

test('a bounded numeric rule is not mislabeled as pass-through', async () => {
  const model = await import('./pointProcessingTemplateEditorModel.ts')

  assert.equal(model.visualTransformKind({
    kind: 'numeric', input: 'power', scale: 1, offset: 0, minimum: -500, maximum: 500,
  }), 'numeric')
})
