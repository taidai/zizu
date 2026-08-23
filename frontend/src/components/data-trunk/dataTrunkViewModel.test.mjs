import assert from 'node:assert/strict'
import test from 'node:test'

test('blockers disable apply and expose one concrete next action', async () => {
  const viewModel = await import('./dataTrunkViewModel.ts')
  const model = viewModel.buildDataTrunkViewModel({
    plan: {
      status: 'blocked',
      items: [],
      blockers: [{
        code: 'POINT_PROCESSING_INPUT_AMBIGUOUS',
        input_id: 'operating_state_raw',
      }],
    },
  })

  assert.equal(model.canApply, false)
  assert.equal(model.nextAction, '请选择“运行状态”对应的原始点位')
  assert.deepEqual(model.counts, { add: 0, update: 0, preserve: 0, delete_candidate: 0, block: 1 })
})

test('quality and invalid-current-value projection are explicit', async () => {
  const viewModel = await import('./dataTrunkViewModel.ts')

  assert.deepEqual(viewModel.projectEntityValue({ value: 18.4, quality: 192 }), {
    currentValue: '18.4',
    qualityLabel: '正常',
    currentUsable: true,
  })
  assert.deepEqual(viewModel.projectEntityValue({ value: 18.4, quality: 0 }), {
    currentValue: '无当前值',
    qualityLabel: '无效',
    currentUsable: false,
  })
  assert.deepEqual(viewModel.projectEntityValue({ value: 18.4, quality: 64 }), {
    currentValue: '18.4',
    qualityLabel: '存疑',
    currentUsable: true,
  })
})

test('delete candidate explains runtime stop without erasing history', async () => {
  const viewModel = await import('./dataTrunkViewModel.ts')
  assert.equal(
    viewModel.planActionLabel('delete_candidate'),
    '应用后停止生成新的 L2 观测；历史值与来源证据保留',
  )
})

test('delivery wizard exposes five steps and the L0 L1 L2 backbone', async () => {
  const viewModel = await import('./dataTrunkViewModel.ts')
  const view = viewModel.buildDataTrunkViewModel({
    plan: {
      status: 'blocked',
      items: [
        { action: 'add', layer: 'L0' },
        { action: 'add', layer: 'L1' },
        { action: 'block', layer: 'L2' },
      ],
      blockers: [{ code: 'NEURON_POINT_ADDRESS_DUPLICATE' }],
    },
  })

  assert.equal(view.steps.map((step) => step.key).join(','), 'target,scan,preview,apply,acceptance')
  assert.deepEqual(view.layers, ['L0', 'L1', 'L2'])
  assert.equal(view.canApply, false)
  assert.equal(view.labels.l1, 'L1 点位加工')
  assert.deepEqual(view.layerCounts, { L0: 1, L1: 1, L2: 1 })
})

test('formula preview exposes one typed selector and DAG summary', async () => {
  const viewModel = await import('./dataTrunkViewModel.ts')
  const model = viewModel.buildFormulaPreviewViewModel({
    expression: 'sum(pcs_power)',
    ast_digest: 'a'.repeat(64),
    result_type: 'FLOAT',
    result_unit: 'kW',
    member_count: 2,
    selector_members: [],
    dag_summary: { edge_count: 2, max_depth: 2, digest: 'b'.repeat(64) },
    blockers: [],
  })

  assert.equal(model.resultContract, 'FLOAT · kW')
  assert.equal(model.memberLabel, '已冻结 2 个输入实体')
  assert.equal(model.dagLabel, '2 条依赖 · 深度 2/8')
  assert.equal(model.ready, true)
})
