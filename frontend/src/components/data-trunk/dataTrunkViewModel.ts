export type DataTrunkQuality = 0 | 1 | 64 | 192
export type PointProcessingPlanAction = 'add' | 'update' | 'preserve' | 'delete_candidate' | 'block'

interface PlanLike {
  status: 'ready' | 'blocked' | 'applied'
  items: { action?: string; layer?: string }[]
  blockers: { code: string; input_id?: string; input_key?: string }[]
}

export const DATA_TRUNK_STEPS = [
  { key: 'target', label: '选择设备' },
  { key: 'scan', label: '只读扫描' },
  { key: 'preview', label: '统一预览' },
  { key: 'apply', label: '原子应用' },
] as const

export const DATA_TRUNK_LAYERS = ['L0', 'L1', 'L2'] as const

export const RAW_POINT_COLUMNS = [
  '点位名称',
  '当前值',
  '单位',
  '质量',
  '数据时间',
  '来源',
] as const

export const RAW_HISTORY_INITIAL_SELECTION: string | null = null

export const POINT_PROCESSING_ACTIONS = {
  inspect: '检查加工结果',
  publish: '检查并发布',
  inspecting: '正在检查...',
  publishing: '正在发布...',
} as const

export const ENTITY_HISTORY_RANGES = [
  ['1h', '1小时'],
  ['6h', '6小时'],
  ['24h', '24小时'],
  ['7d', '7天'],
] as const

export type NodeDataTabKey = 'raw-points' | 'entities'

export interface NodeDataTab {
  key: NodeDataTabKey
  label: string
}

export function nodeDataTabs(_readOnly: boolean): readonly NodeDataTab[] {
  return [
    { key: 'raw-points', label: '原始数据' },
    { key: 'entities', label: '标准实体' },
  ]
}

interface TemplateCandidate {
  revision_id: string
  revision: number
  inputs: Array<{
    source_kind: 'l0' | 'l2'
    source_key: string
    aliases: string[]
    data_type: string
    unit: string | null
    required: boolean
  }>
}

interface L0Candidate {
  source_key: string
  data_type: string
  unit: string | null
}

export function recommendPointProcessingTemplate(
  templates: TemplateCandidate[],
  l0: L0Candidate[],
  installedRevisionId: string | null,
): string {
  if (installedRevisionId && templates.some((item) => item.revision_id === installedRevisionId)) {
    return installedRevisionId
  }
  const score = (template: TemplateCandidate) => template.inputs.reduce((total, input) => {
    if (input.source_kind !== 'l0') return total
    const keys = new Set([input.source_key, ...input.aliases].map((item) => item.toLocaleLowerCase()))
    const matches = l0.filter((source) => (
      keys.has(source.source_key.toLocaleLowerCase())
      && source.data_type === input.data_type
      && (source.unit || null) === (input.unit || null)
    ))
    return total + (matches.length === 1 ? 10 : matches.length > 1 ? 1 : input.required ? -100 : 0)
  }, 0)
  return [...templates]
    .sort((left, right) => score(right) - score(left)
      || right.revision - left.revision
      || left.revision_id.localeCompare(right.revision_id))[0]?.revision_id || ''
}

export function processingKindLabel(kind: string | null): '即时' | '统计' | '未标注' {
  if (!kind) return '未标注'
  return kind === 'window' || kind === 'metric' || kind === 'statistics' ? '统计' : '即时'
}

export function isCurrentNodeResult(
  resultNodeId: string | undefined,
  currentNodeId: string,
): boolean {
  return Boolean(resultNodeId) && resultNodeId === currentNodeId
}

export function manualBindableInputs<T extends {
  source_kind: 'l0' | 'l2'
  selector?: unknown
}>(inputs: T[]): T[] {
  return inputs.filter((input) => input.source_kind === 'l0' && !input.selector)
}

export function selectedInputBindings(
  selections: Record<string, string>,
): Record<string, string> {
  return Object.fromEntries(Object.entries(selections).filter(([, sourceId]) => sourceId))
}

export function scannedInputCandidates(
  items: Array<{
    kind: string
    input_id?: string
    after?: {
      source_id: string
      name: string
      value_data_type: string
      unit: string | null
      group: string
      source_address: string
    } | null
  }>,
  inputId: string,
): Array<{
  source_id: string
  source_key: string
  data_type: string
  unit: string | null
  group: string
  source_address: string
}> {
  return items
    .filter((item) => item.kind === 'l0_point' && item.input_id === inputId && item.after)
    .map((item) => ({
      source_id: item.after!.source_id,
      source_key: item.after!.name,
      data_type: item.after!.value_data_type,
      unit: item.after!.unit,
      group: item.after!.group,
      source_address: item.after!.source_address,
    }))
}

export function pointCandidateLabel(source: {
  source_key: string
  unit: string | null
  group?: string
  source_address?: string
}): string {
  const location = source.group && source.source_address
    ? ` · ${source.group} · ${source.source_address}`
    : ''
  return `${source.source_key}${location}${source.unit ? `（${source.unit}）` : ''}`
}

export function entityFrameEvidence(
  observationFrameSequence: number | null | undefined,
  projectionFrameSequence: number | null | undefined,
): { observationFrameSequence: number | null; projectionFrameSequence: number | null } {
  return {
    observationFrameSequence: observationFrameSequence ?? null,
    projectionFrameSequence: projectionFrameSequence ?? null,
  }
}

export function entityReasonLabel(reason: string | null, ageMs: number): string | null {
  if (reason === 'FRAME_PROCESSING_FAILED') return '本次点位加工失败，当前值不可用'
  if (reason === 'STALE' || reason === 'ENTITY_DATA_STALE') {
    const minutes = Math.max(1, Math.floor(ageMs / 60_000))
    return `原始数据已 ${minutes} 分钟未更新`
  }
  if (reason === 'ENTITY_DATA_QUALITY_BAD') return '原始数据质量异常，当前值不可用'
  return reason ? '当前值不可用，请展开技术详情' : null
}

const INPUT_LABELS: Record<string, string> = {
  active_power_raw: '有功功率',
  operating_state_raw: '运行状态',
  fault_codes_raw: '故障码',
}

const BLOCKER_ACTIONS: Record<string, (input: string) => string> = {
  POINT_PROCESSING_INPUT_AMBIGUOUS: (input) => `请选择“${input}”对应的原始点位`,
  POINT_PROCESSING_INPUT_MISSING: (input) => `请先采集“${input}”原始点位`,
  POINT_PROCESSING_INPUT_INCOMPATIBLE: (input) => `请修正“${input}”的数据类型或单位`,
  POINT_PROCESSING_INPUT_SELECTION_INVALID: (input) => `请重新选择“${input}”原始点位`,
  POINT_PROCESSING_OUTPUT_CONTRACT_MISMATCH: () => '输出实体契约已变化，请重新发布点位加工',
}

export function qualityLabel(quality: number): string {
  if (quality === 192) return '正常'
  if (quality === 64) return '存疑'
  if (quality === 1) return '超时'
  return '无效'
}

export function projectRawPointValue(
  value: unknown,
  quality: number,
  available = true,
): {
  displayValue: string
  qualityLabel: string
  qualityTone: 'good' | 'uncertain' | 'stale' | 'bad'
} {
  if (!available) {
    return {
      displayValue: '-',
      qualityLabel: '平台暂不可用',
      qualityTone: 'bad',
    }
  }
  return {
    displayValue: value === null || value === undefined
      ? '-'
      : Array.isArray(value) ? value.join('、') : String(value),
    qualityLabel: qualityLabel(quality),
    qualityTone: quality === 192
      ? 'good'
      : quality === 64
        ? 'uncertain'
        : quality === 1 ? 'stale' : 'bad',
  }
}

export function projectEntityValue(observation: { value: unknown; quality: number }): {
  currentValue: string
  qualityLabel: string
  currentUsable: boolean
} {
  const currentUsable = observation.quality === 192 || observation.quality === 64
  const value = observation.value
  return {
    currentValue: currentUsable
      ? Array.isArray(value) ? value.join('、') : value === null ? '无当前值' : String(value)
      : '无当前值',
    qualityLabel: qualityLabel(observation.quality),
    currentUsable,
  }
}

export function planActionLabel(action: PointProcessingPlanAction): string {
  const labels: Record<PointProcessingPlanAction, string> = {
    add: '新增',
    update: '更新',
    preserve: '保持',
    delete_candidate: '应用后停止生成新的实体数据；历史值与来源证据保留',
    block: '阻断',
  }
  return labels[action]
}

export function buildDataTrunkViewModel({ plan }: { plan: PlanLike | null }) {
  const counts: Record<PointProcessingPlanAction, number> = {
    add: 0,
    update: 0,
    preserve: 0,
    delete_candidate: 0,
    block: 0,
  }
  for (const item of plan?.items || []) {
    if (item.action && item.action in counts) counts[item.action as PointProcessingPlanAction] += 1
  }
  const layerCounts = { L0: 0, L1: 0, L2: 0 }
  for (const item of plan?.items || []) {
    if (item.layer === 'L0' || item.layer === 'L1' || item.layer === 'L2') {
      layerCounts[item.layer] += 1
    }
  }
  if (plan?.blockers.length && counts.block === 0) counts.block = plan.blockers.length
  const blocker = plan?.blockers[0]
  const inputId = blocker?.input_id || blocker?.input_key || '目标输入'
  const inputLabel = INPUT_LABELS[inputId] || inputId
  const nextAction = blocker
    ? (BLOCKER_ACTIONS[blocker.code] || (() => '请处理计划中的阻断项'))(inputLabel)
    : plan?.status === 'ready'
      ? '核对变更后应用点位加工'
      : plan?.status === 'applied'
        ? '查看实体实时值与来源证据'
        : '选择点位加工模板并生成计划'
  return {
    steps: DATA_TRUNK_STEPS,
    layers: [...DATA_TRUNK_LAYERS],
    labels: { l0: 'L0 原始点位', l1: 'L1 点位加工', l2: 'L2 实体数据' },
    canApply: plan?.status === 'ready' && !plan.blockers.length,
    nextAction,
    counts,
    layerCounts,
  }
}

export function buildFormulaPreviewViewModel(preview: {
  result_type: string
  result_unit: string | null
  member_count: number
  dag_summary: { edge_count: number; max_depth: number | null }
  blockers: Array<{ code: string }>
}) {
  return {
    resultContract: `${preview.result_type}${preview.result_unit ? ` · ${preview.result_unit}` : ''}`,
    memberLabel: `已冻结 ${preview.member_count} 个输入实体`,
    dagLabel: `${preview.dag_summary.edge_count} 条依赖 · 深度 ${preview.dag_summary.max_depth ?? '未计算'}/8`,
    ready: preview.blockers.length === 0,
  }
}

export type VisualFormulaFunction = 'sum' | 'avg' | 'min_of' | 'max_of' | 'count'

const visualFormulaFunctions = new Set<VisualFormulaFunction>([
  'sum',
  'avg',
  'min_of',
  'max_of',
  'count',
])

export function buildVisualFormula(
  functionName: VisualFormulaFunction,
  inputId: string,
): string {
  if (!visualFormulaFunctions.has(functionName) || !/^[A-Za-z_]\w*$/.test(inputId)) {
    throw new Error('可视化公式参数无效')
  }
  return `${functionName}(${inputId})`
}

export function parseVisualFormula(
  expression: string,
  inputIds: string[],
): { functionName: VisualFormulaFunction; inputId: string } | null {
  const matched = expression.match(
    /^\s*(sum|avg|min_of|max_of|count)\(\s*([A-Za-z_]\w*)\s*\)\s*$/,
  )
  if (!matched || !inputIds.includes(matched[2])) return null
  return {
    functionName: matched[1] as VisualFormulaFunction,
    inputId: matched[2],
  }
}
