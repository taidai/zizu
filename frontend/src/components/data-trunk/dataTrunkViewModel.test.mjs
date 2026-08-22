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
