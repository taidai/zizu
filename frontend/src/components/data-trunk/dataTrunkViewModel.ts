export type DataTrunkQuality = 0 | 1 | 64 | 192
export type PointConversionPlanAction = 'add' | 'update' | 'preserve' | 'delete_candidate' | 'block'

interface PlanLike {
  status: 'ready' | 'blocked' | 'applied'
  items: { action?: string }[]
  blockers: { code: string; input_id?: string; input_key?: string }[]
}

const INPUT_LABELS: Record<string, string> = {
  active_power_raw: '有功功率',
  operating_state_raw: '运行状态',
  fault_codes_raw: '故障码',
}

const BLOCKER_ACTIONS: Record<string, (input: string) => string> = {
  POINT_CONVERSION_INPUT_AMBIGUOUS: (input) => `请选择“${input}”对应的原始点位`,
  POINT_CONVERSION_INPUT_MISSING: (input) => `请先采集“${input}”原始点位`,
  POINT_CONVERSION_INPUT_INCOMPATIBLE: (input) => `请修正“${input}”的数据类型或单位`,
  POINT_CONVERSION_INPUT_SELECTION_INVALID: (input) => `请重新选择“${input}”原始点位`,
  POINT_CONVERSION_OUTPUT_CONTRACT_MISMATCH: () => '输出实体契约已变化，请重新安装解决方案',
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

export function planActionLabel(action: PointConversionPlanAction): string {
  const labels: Record<PointConversionPlanAction, string> = {
    add: '新增',
    update: '更新',
    preserve: '保持',
    delete_candidate: '应用后停止生成新的 L2 观测；历史值与来源证据保留',
    block: '阻断',
  }
  return labels[action]
}

export function buildDataTrunkViewModel({ plan }: { plan: PlanLike | null }) {
  const counts: Record<PointConversionPlanAction, number> = {
    add: 0,
    update: 0,
    preserve: 0,
    delete_candidate: 0,
    block: 0,
  }
  for (const item of plan?.items || []) {
    if (item.action && item.action in counts) counts[item.action as PointConversionPlanAction] += 1
  }
  if (plan?.blockers.length && counts.block === 0) counts.block = plan.blockers.length
  const blocker = plan?.blockers[0]
  const inputId = blocker?.input_id || blocker?.input_key || '目标输入'
  const inputLabel = INPUT_LABELS[inputId] || inputId
  const nextAction = blocker
    ? (BLOCKER_ACTIONS[blocker.code] || (() => '请处理计划中的阻断项'))(inputLabel)
    : plan?.status === 'ready'
      ? '核对变更后应用点位转换'
      : plan?.status === 'applied'
        ? '查看全局实体实时值与验收证据'
        : '选择点位转换模板并生成计划'
  return {
    canApply: plan?.status === 'ready' && !plan.blockers.length,
    nextAction,
    counts,
  }
}
