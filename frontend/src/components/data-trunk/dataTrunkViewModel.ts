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
  { key: 'acceptance', label: '机器验收' },
] as const

export const DATA_TRUNK_LAYERS = ['L0', 'L1', 'L2'] as const

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
  POINT_PROCESSING_OUTPUT_CONTRACT_MISMATCH: () => '输出实体契约已变化，请重新安装解决方案',
}

export function qualityLabel(quality: number): string {
  if (quality === 192) return '正常'
  if (quality === 64) return '存疑'
  if (quality === 1) return '超时'
  return '无效'
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
    delete_candidate: '应用后停止生成新的 L2 观测；历史值与来源证据保留',
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
        ? '查看全局实体实时值与验收证据'
        : '选择点位加工模板并生成计划'
  return {
    steps: DATA_TRUNK_STEPS,
    layers: [...DATA_TRUNK_LAYERS],
    labels: { l0: 'L0 原始点位', l1: 'L1 点位加工', l2: 'L2 全局实体' },
    canApply: plan?.status === 'ready' && !plan.blockers.length,
    nextAction,
    counts,
    layerCounts,
  }
}
