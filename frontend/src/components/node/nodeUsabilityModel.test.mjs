import assert from 'node:assert/strict'
import test from 'node:test'

test('parent candidates exclude the edited node and its descendants', async () => {
  const model = await import('./nodeUsabilityModel.ts')
  const nodes = [
    { id: 'site', parent_id: null, layer: 1, name: '站点' },
    { id: 'storage', parent_id: 'site', layer: 2, name: '储能' },
    { id: 'pcs', parent_id: 'storage', layer: 3, name: 'PCS' },
    { id: 'other', parent_id: 'site', layer: 2, name: '光伏' },
  ]

  assert.deepEqual(
    model.parentCandidates(nodes, 'storage').map((item) => item.id),
    ['site', 'other'],
  )
})

test('import summary blocks apply on conflict and explains all actions', async () => {
  const model = await import('./nodeUsabilityModel.ts')

  assert.deepEqual(
    model.importPreviewSummary({
      counts: { create: 2, update: 3, unchanged: 4, conflict: 1 },
      has_conflicts: true,
    }),
    {
      create: 2,
      update: 3,
      unchanged: 4,
      conflict: 1,
      canApply: false,
      label: '新增 2 · 更新 3 · 不变 4 · 冲突 1',
    },
  )
})

test('group selection is unique and stable', async () => {
  const model = await import('./nodeUsabilityModel.ts')
  assert.deepEqual(model.normalizedGroups(['status', 'data', 'status', '']), ['data', 'status'])
})
