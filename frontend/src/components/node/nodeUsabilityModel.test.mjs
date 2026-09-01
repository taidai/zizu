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

test('raw point selection exposes only maintenance actions that can change state', async () => {
  const model = await import('./nodeUsabilityModel.ts')

  assert.deepEqual(
    model.rawPointSelectionSummary([
      { id: 'enabled', enabled: true },
      { id: 'disabled', enabled: false },
    ]),
    {
      count: 2,
      canEditDisplayName: false,
      canEnable: true,
      canDisable: true,
      canDelete: true,
    },
  )
  assert.deepEqual(
    model.rawPointSelectionSummary([{ id: 'one', enabled: true }]),
    {
      count: 1,
      canEditDisplayName: true,
      canEnable: false,
      canDisable: true,
      canDelete: true,
    },
  )
  assert.deepEqual(
    model.rawPointSelectionSummary([]),
    {
      count: 0,
      canEditDisplayName: false,
      canEnable: false,
      canDisable: false,
      canDelete: false,
    },
  )
})

test('raw point display-name change trims input and rejects an empty name', async () => {
  const model = await import('./nodeUsabilityModel.ts')

  assert.deepEqual(
    model.rawPointDisplayNameChange('point-1', '  PCS 有功功率  '),
    { tagIds: ['point-1'], changes: { display_name: 'PCS 有功功率' } },
  )
  assert.throws(
    () => model.rawPointDisplayNameChange('point-1', '   '),
    /请输入点位显示名称/,
  )
})
