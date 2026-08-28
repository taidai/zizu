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

test('raw point keeps the last diagnostic value while stale', async () => {
  const viewModel = await import('./dataTrunkViewModel.ts')

  assert.deepEqual(viewModel.projectRawPointValue(34.1, 1), {
    displayValue: '34.1',
    qualityLabel: '超时',
    qualityTone: 'stale',
  })
  assert.deepEqual(viewModel.projectRawPointValue(null, 1), {
    displayValue: '-',
    qualityLabel: '超时',
    qualityTone: 'stale',
  })
})

test('delete candidate explains runtime stop without erasing history', async () => {
  const viewModel = await import('./dataTrunkViewModel.ts')
  assert.equal(
    viewModel.planActionLabel('delete_candidate'),
    '应用后停止生成新的实体数据；历史值与来源证据保留',
  )
})

test('delivery wizard exposes the four-step L0 L1 L2 backbone', async () => {
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

  assert.equal(view.steps.map((step) => step.key).join(','), 'target,scan,preview,apply')
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
  assert.equal(viewModel.buildFormulaPreviewViewModel({
    result_type: 'FLOAT',
    result_unit: null,
    member_count: 0,
    dag_summary: { edge_count: 0, max_depth: null },
    blockers: [],
  }).dagLabel, '0 条依赖 · 深度 未计算/8')
})

test('visual formula builder and text parser share one canonical expression', async () => {
  const viewModel = await import('./dataTrunkViewModel.ts')

  assert.equal(
    viewModel.buildVisualFormula('avg', 'pcs_power'),
    'avg(pcs_power)',
  )
  assert.deepEqual(
    viewModel.parseVisualFormula('avg(pcs_power)', ['pcs_power', 'reserve_power']),
    { functionName: 'avg', inputId: 'pcs_power' },
  )
  assert.equal(
    viewModel.parseVisualFormula('pcs_power + reserve_power', ['pcs_power', 'reserve_power']),
    null,
  )
})

test('node data tabs use task names and hide processing from operators', async () => {
  const model = await import('./dataTrunkViewModel.ts')
  assert.deepEqual(
    model.nodeDataTabs(false).map((item) => [item.key, item.label]),
    [
      ['raw-points', '原始点位'],
      ['point-processing', '点位加工'],
      ['entities', '实体数据'],
    ],
  )
  assert.deepEqual(
    model.nodeDataTabs(true).map((item) => item.key),
    ['raw-points', 'entities'],
  )
  assert.deepEqual(model.RAW_POINT_COLUMNS, [
    '点位名称', '当前值', '单位', '质量', '数据时间', '来源',
  ])
})

test('template recommendation keeps installed revision then prefers exact coverage', async () => {
  const model = await import('./dataTrunkViewModel.ts')
  const templates = [
    { revision_id: 'a', revision: 1, inputs: [{ source_kind: 'l0', source_key: 'P', aliases: [], data_type: 'FLOAT', unit: 'W', required: true }] },
    { revision_id: 'b', revision: 1, inputs: [{ source_kind: 'l0', source_key: 'Power', aliases: ['PAct'], data_type: 'FLOAT', unit: 'kW', required: true }] },
  ]
  const l0 = [{ source_key: 'Power', data_type: 'FLOAT', unit: 'kW' }]
  assert.equal(model.recommendPointProcessingTemplate(templates, l0, 'a'), 'a')
  assert.equal(model.recommendPointProcessingTemplate(templates, l0, null), 'b')
  assert.deepEqual(model.POINT_PROCESSING_ACTIONS, {
    inspect: '检查加工结果',
    publish: '检查并发布',
    inspecting: '正在检查...',
    publishing: '正在发布...',
  })
})

test('entity reason is human readable and technical kind stays a label', async () => {
  const model = await import('./dataTrunkViewModel.ts')
  assert.deepEqual(model.ENTITY_HISTORY_RANGES, [
    ['1h', '1小时'],
    ['6h', '6小时'],
    ['24h', '24小时'],
    ['7d', '7天'],
  ])
  assert.equal(model.processingKindLabel('window'), '统计')
  assert.equal(model.processingKindLabel('formula'), '即时')
  assert.equal(model.processingKindLabel(null), '未标注')
  assert.equal(model.entityReasonLabel('FRAME_PROCESSING_FAILED', 0), '本次点位加工失败，当前值不可用')
  assert.equal(model.entityReasonLabel('STALE', 17 * 60_000), '原始数据已 17 分钟未更新')
})

test('async results fail closed when the selected node changed', async () => {
  const model = await import('./dataTrunkViewModel.ts')
  assert.equal(model.isCurrentNodeResult('node-a', 'node-a'), true)
  assert.equal(model.isCurrentNodeResult('node-a', 'node-b'), false)
  assert.equal(model.isCurrentNodeResult(undefined, 'node-a'), false)
})

test('scan templates still allow manual binding and raw history starts idle', async () => {
  const model = await import('./dataTrunkViewModel.ts')
  assert.deepEqual(model.manualBindableInputs([
    { input_id: 'power', source_kind: 'l0', source_contract: { plugin: 'modbus' } },
    { input_id: 'site_power', source_kind: 'l2', selector: { scope: 'descendants' } },
  ]).map((input) => input.input_id), ['power'])
  assert.deepEqual(model.selectedInputBindings({ power: 'source-1', state: '' }), {
    power: 'source-1',
  })
  assert.deepEqual(model.scannedInputCandidates([
    { kind: 'l0_point', input_id: 'power', after: { source_id: 'candidate-1', name: '功率 A', value_data_type: 'FLOAT', unit: 'kW', group: 'data-a', source_address: '1!1' } },
    { kind: 'l0_point', input_id: 'state', after: { source_id: 'candidate-2', name: '状态', value_data_type: 'STRING', unit: null, group: 'data-b', source_address: '1!2' } },
  ], 'power'), [{ source_id: 'candidate-1', source_key: '功率 A', data_type: 'FLOAT', unit: 'kW', group: 'data-a', source_address: '1!1' }])
  assert.equal(model.pointCandidateLabel({
    source_key: '总有功功率', unit: 'kW', group: 'data-a', source_address: '1!416409',
  }), '总有功功率 · data-a · 1!416409（kW）')
  assert.deepEqual(model.entityFrameEvidence(7, 9), {
    observationFrameSequence: 7,
    projectionFrameSequence: 9,
  })
  assert.equal(model.RAW_HISTORY_INITIAL_SELECTION, null)
})
