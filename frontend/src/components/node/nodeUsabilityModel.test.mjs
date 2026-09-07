import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
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

test('point catalog scope changes reset page and visible selection while preserving page size options', async () => {
  const model = await import('./nodeUsabilityModel.ts')
  const current = {
    nodeId: 'node-a', page: 3, pageSize: 20, search: '', dataType: '', selectedIds: ['point-a'],
  }

  assert.deepEqual(model.changePointCatalogScope(current, { search: 'power' }), {
    nodeId: 'node-a', page: 1, pageSize: 20, search: 'power', dataType: '', selectedIds: [],
  })
  assert.deepEqual(model.changePointCatalogScope(current, { nodeId: 'node-b' }), {
    nodeId: 'node-b', page: 1, pageSize: 20, search: '', dataType: '', selectedIds: [],
  })
  assert.deepEqual(model.changePointCatalogScope(current, { pageSize: 10 }), {
    nodeId: 'node-a', page: 1, pageSize: 10, search: '', dataType: '', selectedIds: [],
  })
})

test('neuron preview exposes every item action reason and source identity', async () => {
  const model = await import('./nodeUsabilityModel.ts')

  assert.deepEqual(model.importPreviewRows({
    items: [
      { source_path: 'n/g/P', group: 'g', name: 'P', source_address: '1!1', action: 'create', reason: null },
      { source_path: 'n/g/S', group: 'g', name: 'S', source_address: '1!2', action: 'conflict', reason: '地址重复' },
    ],
  }), [
    { key: 'n/g/P', source: 'g · P · 1!1', action: 'create', actionLabel: '新增', reason: '—' },
    { key: 'n/g/S', source: 'g · S · 1!2', action: 'conflict', actionLabel: '冲突', reason: '地址重复' },
  ])
})

test('initial node is selected only after the fetched catalog contains it', async () => {
  const model = await import('./nodeUsabilityModel.ts')
  const nodes = [{ id: 'root', parent_id: null }, { id: 'pcs', parent_id: 'root' }]

  assert.equal(model.initialNodeSelection(nodes, '', 'pcs'), 'pcs')
  assert.equal(model.initialNodeSelection(nodes, '', 'missing'), 'root')
  assert.equal(model.initialNodeSelection([], '', 'pcs'), '')
  assert.equal(model.initialNodeSelection(nodes, 'root', 'pcs'), 'root')
})

test('neuron preview belongs only to the captured generation node and sorted groups', async () => {
  const model = await import('./nodeUsabilityModel.ts')
  const captured = model.captureNeuronSelection(4, 'neuron-a', ['status', 'data'])
  const response = {
    node_id: 'zizu-node',
    neuron_node: 'neuron-a',
    selected_groups: ['data', 'status'],
  }

  assert.deepEqual(captured, { generation: 4, neuronNode: 'neuron-a', groups: ['data', 'status'] })
  assert.equal(model.neuronPreviewBelongsToSelection(response, captured, captured), true)
  assert.equal(model.neuronPreviewBelongsToSelection(response, captured, { ...captured, generation: 5 }), false)
  assert.equal(model.neuronPreviewBelongsToSelection(response, captured, { ...captured, neuronNode: 'neuron-b' }), false)
  assert.equal(model.neuronPreviewBelongsToSelection(response, captured, { ...captured, groups: ['data'] }), false)
  assert.equal(model.neuronPreviewBelongsToSelection({ ...response, selected_groups: ['data'] }, captured, captured), false)
})

test('unknown neuron import is considered reconciled only when fresh preview has no writes', async () => {
  const model = await import('./nodeUsabilityModel.ts')

  assert.equal(model.neuronImportReconciliation({ counts: { unchanged: 3 }, has_conflicts: false }), 'applied')
  assert.equal(model.neuronImportReconciliation({ counts: { create: 1 }, has_conflicts: false }), 'fresh-preview')
  assert.equal(model.neuronImportReconciliation({ counts: { update: 1 }, has_conflicts: false }), 'fresh-preview')
  assert.equal(model.neuronImportReconciliation({ counts: { conflict: 1 }, has_conflicts: true }), 'fresh-preview')
})

test('neuron import UI wires selection generations and unknown-result reconciliation reads', async () => {
  const source = await readFile(new URL('../../pages/NodeTreePage.tsx', import.meta.url), 'utf8')

  assert.match(source, /selectionGenerationRef/)
  assert.match(source, /neuronPreviewBelongsToSelection/)
  assert.match(source, /NeuronImportResultUnknownError/)
  assert.match(source, /Promise\.all\(\[\s*fetchNodes\(\),\s*fetchTags\(node\.id, 1, 1\),\s*previewNeuronTags/s)
  assert.match(source, /当前已禁止重复写入/)
})
