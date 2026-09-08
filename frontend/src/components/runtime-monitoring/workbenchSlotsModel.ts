import type { EntityInstance, EmsWorkbenchSlot } from '../../api/client'

export const FIXED_WORKBENCH_SLOTS = [
  { id: 'site-power', label: '站点功率', unit: 'kW' },
  { id: 'pv-power', label: '光伏功率', unit: 'kW' },
  { id: 'storage-power', label: '储能功率', unit: 'kW' },
  { id: 'storage-soc', label: '储能 SOC', unit: '%' },
  { id: 'charging-power', label: '充电功率', unit: 'kW' },
] as const

export type WorkbenchSlotKey = typeof FIXED_WORKBENCH_SLOTS[number]['id']

export type WorkbenchSlotView = EmsWorkbenchSlot & {
  id: WorkbenchSlotKey
  reading: {
    kind: 'current' | 'last' | 'missing'
    valueText: string
    numericValue: number | null
    unit: string
    quality: number | null
    observedAt: string | null
    reason: string | null
  }
  bindingLabel: string
}

export type WorkbenchRuntimeEvidence = {
  entityInstanceId: string
  observation: {
    value: unknown
    unit: string | null
    quality: number
    reason: string | null
    observed_at: string | null
    value_observed_at: string | null
  } | null
  nodeCurrent: boolean
  reason?: string
}

const bindingLabels: Record<EmsWorkbenchSlot['binding_mode'], string> = {
  manual: '人工绑定',
  exact: '自动匹配',
  unconfigured: '未配置',
  ambiguous: '需人工选择',
}

function valueText(value: unknown): string {
  if (typeof value === 'number' && Number.isFinite(value)) return value.toFixed(1)
  if (typeof value === 'string' && value.trim()) return value
  return '—'
}

export function buildWorkbenchSlots(
  kpis: readonly EmsWorkbenchSlot[],
  runtimeEvidence: readonly WorkbenchRuntimeEvidence[] = [],
  workbenchCurrent = true,
): WorkbenchSlotView[] {
  const byId = new Map(kpis.map((slot) => [slot.id, slot]))
  const evidenceByEntity = new Map(runtimeEvidence.map((item) => [item.entityInstanceId, item]))
  return FIXED_WORKBENCH_SLOTS.map((fixed) => {
    const wire = byId.get(fixed.id)
    const slot: EmsWorkbenchSlot = wire || {
      id: fixed.id,
      label: fixed.label,
      binding_mode: 'unconfigured',
      reason: '工作台接口未返回此固定槽位。',
      entity: null,
    }
    const entity = slot.entity
    const runtime = entity ? evidenceByEntity.get(entity.entity_instance_id) : undefined
    const observation = runtime?.observation
    const runtimeObservationMissing = Boolean(runtime) && !observation
    const value = observation ? observation.value : entity?.value
    const quality = observation ? observation.quality : entity?.quality ?? null
    const hasValue = value !== null && value !== undefined
    const current = Boolean(runtime)
      && Boolean(observation)
      && workbenchCurrent
      && runtime?.nodeCurrent === true
      && entity?.status === 'available'
      && quality === 192
      && typeof value === 'number'
      && Number.isFinite(value)
    const evidenceReason = [
      !workbenchCurrent ? '工作台刷新失败，当前值已降级为最后值。' : null,
      runtimeObservationMissing ? '已提交实时帧缺少与槽位实体匹配的 L2 观测，工作台快照只保留为最后值。' : null,
      runtime?.reason,
      observation?.reason,
    ].filter(Boolean).join('；') || null
    return {
      ...slot,
      id: fixed.id,
      label: slot.label || fixed.label,
      bindingLabel: bindingLabels[slot.binding_mode],
      reading: {
        kind: current ? 'current' : hasValue ? 'last' : 'missing',
        valueText: hasValue ? valueText(value) : '—',
        numericValue: typeof value === 'number' && Number.isFinite(value) ? value : null,
        unit: observation?.unit || entity?.unit || fixed.unit,
        quality,
        observedAt: observation?.value_observed_at || observation?.observed_at || entity?.observed_at || null,
        reason: evidenceReason,
      },
    }
  })
}

export function compatibleSlotEntities(
  slotKey: WorkbenchSlotKey,
  entities: readonly Pick<EntityInstance, 'id' | 'data_type' | 'unit'>[],
): Array<Pick<EntityInstance, 'id' | 'data_type' | 'unit'>> {
  const expectedUnit = slotKey === 'storage-soc' ? '%' : 'kW'
  return entities.filter((entity) => (
    ['float', 'int'].includes(entity.data_type.toLowerCase()) && entity.unit === expectedUnit
  ))
}

export function fixedEnergyFlow(slots: readonly WorkbenchSlotView[]): {
  links: Array<{ from: WorkbenchSlotKey; to: 'site-bus'; direction: 'neutral' | 'to-bus' | 'from-bus' }>
  reason: string
} {
  const available = new Set(slots.map((slot) => slot.id))
  return {
    links: (['pv-power', 'storage-power', 'charging-power', 'site-power'] as WorkbenchSlotKey[])
      .filter((id) => available.has(id))
      .map((from) => {
        const reading = slots.find((slot) => slot.id === from)?.reading
        const value = reading?.numericValue
        const direction = reading?.kind !== 'current' || value == null || value === 0 || from === 'charging-power'
          ? 'neutral' : value > 0 ? 'to-bus' : 'from-bus'
        return { from, to: 'site-bus' as const, direction }
      }),
    reason: '电网＋供电／−返送；储能＋放电／−充电。零值、超时或未知停止流动；充电桩方向未约定，保持静态。电网槽位须绑定并网功率。',
  }
}

export function controlCommandPresentation(command: Pick<{ status: string; code: string }, 'status' | 'code'>): {
  tone: 'waiting' | 'success' | 'danger'
  label: string
  terminal: boolean
} {
  if (command.status === 'readback_confirmed') return { tone: 'success', label: '回读已确认', terminal: true }
  if (command.status === 'mismatch') return { tone: 'danger', label: '回读不一致', terminal: true }
  if (command.status === 'timeout') return { tone: 'danger', label: '回读超时', terminal: true }
  if (command.status === 'failed') return { tone: 'danger', label: '下发失败', terminal: true }
  if (command.status === 'rejected') return { tone: 'danger', label: '命令被拒绝', terminal: true }
  return { tone: 'waiting', label: '等待设备回读', terminal: false }
}

export function isConfirmationExpired(expiresAt: string, now = Date.now()): boolean {
  const deadline = Date.parse(expiresAt)
  return !Number.isFinite(deadline) || now >= deadline
}

export function describeWorkbenchSlotError(error: unknown): string {
  const value = error && typeof error === 'object'
    ? error as { code?: string | null; message?: string }
    : null
  const messages: Record<string, string> = {
    CONFIGURATION_REVISION_STALE: '配置已被其他操作更新，请关闭弹窗、刷新首页后重试。',
    DATA_FRAME_CONFIGURATION_STALE: '首页依据的已提交数据帧已经过期，请刷新首页后重试。',
    CONFIGURATION_RUNTIME_BUSY: '运行配置正在切换，请稍后重试；当前绑定未按成功处理。',
    CONFIGURATION_RUNTIME_DRAIN_TIMEOUT: '运行配置未能在安全时限内排空，当前绑定未按成功处理。',
    CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED: '运行配置需要恢复，请等待平台恢复后再重试。',
    WORKBENCH_SLOT_ENTITY_NOT_AVAILABLE: '所选 L2 已失效或不再可用，请刷新实体目录后重新选择。',
    WORKBENCH_SLOT_IDEMPOTENCY_CONFLICT: '同一操作键对应了不同请求，请关闭弹窗后重新操作。',
  }
  return value?.code && messages[value.code]
    ? messages[value.code]
    : value?.message || '槽位保存请求未完成，当前绑定未按成功处理。'
}
