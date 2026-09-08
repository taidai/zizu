import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchAlarms,
  fetchControlCommand,
  fetchDispatchStrategies,
  fetchEmsWorkbench,
  fetchEntityInstances,
  reconcileControlCommand,
  requestControlConfirmation,
  saveEmsWorkbenchSlot,
  submitControlCommand,
  type Alarm,
  type ControlCommand,
  type ControlConfirmation,
  type DispatchStrategy,
  type EmsWorkbench,
  type EmsWorkbenchSlotKey,
  type EntityInstance,
  type WorkbenchEntity,
} from '../api/client'
import EntityRuntimeDetail from '../components/runtime-monitoring/EntityRuntimeDetail'
import {
  buildRuntimeNodes,
  type RuntimeEntity,
} from '../components/runtime-monitoring/runtimeModel'
import {
  buildWorkbenchSlots,
  compatibleSlotEntities,
  controlCommandPresentation,
  describeWorkbenchSlotError,
  fixedEnergyFlow,
  isConfirmationExpired,
  type WorkbenchSlotView,
  type WorkbenchRuntimeEvidence,
} from '../components/runtime-monitoring/workbenchSlotsModel'
import { useRuntimeNodes } from '../components/runtime-monitoring/useRuntimeNodes'
import '../components/runtime-monitoring/runtime-monitoring.css'
import '../components/runtime-monitoring/workbench.css'

export type RuntimeTab = 'overview' | 'trends' | 'alarms' | 'controls'

export type RuntimeProps = {
  onOpenAlarms: () => void
  onOpenEngineering?: (nodeId?: string) => void
  onOpenDevices?: () => void
  initialTab?: RuntimeTab
}

const SLOT_ICONS: Record<EmsWorkbenchSlotKey, string> = {
  'site-power': '⌂',
  'pv-power': '▧',
  'storage-power': '▤',
  'storage-soc': '▥',
  'charging-power': 'ϟ',
}

function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未记录'
}

function controlError(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

function qualityLabel(quality: number | null): string {
  if (quality === 192) return '正常'
  if (quality === 64) return '超时'
  if (quality === 1) return '未知'
  return quality == null ? '无数据' : `异常（${quality}）`
}

function mergeControlEvidence(workbench: EmsWorkbench): WorkbenchEntity[] {
  const liveById = new Map(
    workbench.groups.flatMap((group) => group.entities).map((entity) => [entity.entity_instance_id, entity]),
  )
  return workbench.controls.entities.map((entity) => ({ ...entity, ...liveById.get(entity.entity_instance_id) }))
}

function Controls({ entities }: { entities: WorkbenchEntity[] }) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [confirmation, setConfirmation] = useState<{
    entity: WorkbenchEntity
    value: unknown
    receipt: ControlConfirmation
    commandKey: string
  } | null>(null)
  const [commands, setCommands] = useState<Record<string, ControlCommand>>({})
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set())
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const pending = useRef(new Set<string>())

  const setBusy = (key: string, busy: boolean) => {
    if (busy) pending.current.add(key)
    else pending.current.delete(key)
    setBusyIds(new Set(pending.current))
  }

  const valueFor = (entity: WorkbenchEntity): unknown | null => {
    const raw = values[entity.entity_instance_id]
    if (raw == null || raw === '') {
      setError(`请先填写 ${entity.display_name} 的目标值。`)
      return null
    }
    const value = entity.data_type === 'bool'
      ? raw === 'true'
      : ['float', 'int'].includes(entity.data_type) ? Number(raw) : raw
    if (typeof value === 'number' && !Number.isFinite(value)) {
      setError('目标值必须是有效数字。')
      return null
    }
    return value
  }

  const refresh = async (entityInstanceId: string, command: ControlCommand) => {
    const key = `readback:${command.id}`
    if (pending.current.has(key)) return
    setBusy(key, true)
    setError('')
    try {
      const reconciled = await reconcileControlCommand(command.id)
      const presentation = controlCommandPresentation(reconciled)
      const current = presentation.terminal ? reconciled : await fetchControlCommand(command.id)
      setCommands((previous) => ({ ...previous, [entityInstanceId]: current }))
      const finalPresentation = controlCommandPresentation(current)
      setMessage(`${finalPresentation.label} · 命令 ${current.id} · ${current.code}`)
    } catch (reason) {
      setError(controlError(reason, '命令回读状态查询失败。'))
    } finally {
      setBusy(key, false)
    }
  }

  const prepare = async (entity: WorkbenchEntity) => {
    const key = `confirm:${entity.entity_instance_id}`
    if (pending.current.has(key)) return
    if (entity.status === 'unavailable' || (entity.quality != null && entity.quality !== 192)) {
      setError(`${entity.display_name} 当前质量不满足控制要求，未申请确认。`)
      return
    }
    const value = valueFor(entity)
    if (value === null) return
    setBusy(key, true)
    setMessage('')
    setError('')
    try {
      const receipt = await requestControlConfirmation(entity.entity_instance_id, value, crypto.randomUUID())
      setConfirmation({ entity, value, receipt, commandKey: crypto.randomUUID() })
    } catch (reason) {
      setError(controlError(reason, '控制二次确认申请失败。'))
    } finally {
      setBusy(key, false)
    }
  }

  const execute = async () => {
    if (!confirmation || pending.current.has('dispatch')) return
    if (isConfirmationExpired(confirmation.receipt.expires_at)) {
      setError('二次确认已过期，请重新申请。')
      return
    }
    setBusy('dispatch', true)
    setMessage('')
    setError('')
    try {
      const currentConfirmation = confirmation
      const command = await submitControlCommand(
        currentConfirmation.entity.entity_instance_id,
        currentConfirmation.value,
        currentConfirmation.receipt.id,
        currentConfirmation.commandKey,
      )
      setCommands((previous) => ({ ...previous, [currentConfirmation.entity.entity_instance_id]: command }))
      setConfirmation(null)
      setMessage(`等待设备回读 · 命令 ${command.id} 已受理，接口受理不代表设备成功。`)
      await refresh(currentConfirmation.entity.entity_instance_id, command)
    } catch (reason) {
      setError(controlError(reason, '控制命令提交失败，未确认设备动作。'))
    } finally {
      setBusy('dispatch', false)
    }
  }

  if (entities.length === 0) return <p className="runtime-empty">当前没有正式控制目录中的可控 L2 全局实体。</p>
  return (
    <div className="workbench-controls">
      <div className="workbench-control-gate" role="note">
        <strong>统一安全门</strong>
        <span>后端在确认与下发时复核权限、上下限、联锁、质量、新鲜度和冷却；接口受理后仍须等待 committed L2 回读。</span>
      </div>
      {entities.map((entity) => {
        const command = commands[entity.entity_instance_id]
        const presentation = command ? controlCommandPresentation(command) : null
        const blocked = entity.status === 'unavailable' || (entity.quality != null && entity.quality !== 192)
        return (
          <article key={entity.entity_instance_id} className="workbench-control-card">
            <header>
              <div><span>{entity.node_name}</span><h3>{entity.display_name}</h3></div>
              <span className={`workbench-control-quality ${blocked ? 'is-blocked' : ''}`}>{blocked ? '质量不满足' : '后端复核'}</span>
            </header>
            <div className="workbench-control-input">
              {entity.data_type === 'bool' ? (
                <select aria-label={`${entity.display_name}目标值`} value={values[entity.entity_instance_id] || ''} onChange={(event) => setValues((current) => ({ ...current, [entity.entity_instance_id]: event.target.value }))} className="neu-input">
                  <option value="">选择目标</option><option value="true">开启</option><option value="false">关闭</option>
                </select>
              ) : (
                <input aria-label={`${entity.display_name}目标值`} value={values[entity.entity_instance_id] || ''} onChange={(event) => setValues((current) => ({ ...current, [entity.entity_instance_id]: event.target.value }))} type={['float', 'int'].includes(entity.data_type) ? 'number' : 'text'} className="neu-input" placeholder={entity.unit || '目标值'} />
              )}
              <span>{entity.unit || '无单位'}</span>
              <button type="button" disabled={blocked || busyIds.has(`confirm:${entity.entity_instance_id}`)} onClick={() => void prepare(entity)} className="zizu-primary runtime-touch-button">{busyIds.has(`confirm:${entity.entity_instance_id}`) ? '申请中…' : '申请二次确认'}</button>
            </div>
            {command && presentation && (
              <div className={`workbench-command-state is-${presentation.tone}`}>
                <div><strong>{presentation.label}</strong><span>{command.code}</span></div>
                <button type="button" disabled={busyIds.has(`readback:${command.id}`) || presentation.terminal} onClick={() => void refresh(entity.entity_instance_id, command)} className="neu-btn runtime-touch-button">刷新回读</button>
              </div>
            )}
          </article>
        )
      })}
      {confirmation && (() => {
        const expired = isConfirmationExpired(confirmation.receipt.expires_at)
        return (
          <div className="runtime-detail-backdrop" role="presentation">
            <section role="alertdialog" aria-modal="true" aria-label="确认控制命令" className="workbench-confirmation neu-card">
              <p className="runtime-eyebrow">高风险操作 · 二次确认</p>
              <h3>确认控制命令</h3>
              <dl><div><dt>控制对象</dt><dd>{confirmation.entity.node_name} · {confirmation.entity.display_name}</dd></div><div><dt>目标值</dt><dd>{String(confirmation.value)} {confirmation.entity.unit || ''}</dd></div><div><dt>有效期</dt><dd>{formatTime(confirmation.receipt.expires_at)}{expired ? ' · 已过期' : ''}</dd></div></dl>
              <p>确认下发仅表示向统一控制运行时提交命令；设备成功必须以后端 committed L2 回读为准。</p>
              <div><button type="button" disabled={expired || busyIds.has('dispatch')} onClick={() => void execute()} className="runtime-danger runtime-touch-button">{busyIds.has('dispatch') ? '下发中…' : '确认下发'}</button><button type="button" disabled={busyIds.has('dispatch')} onClick={() => setConfirmation(null)} className="neu-btn runtime-touch-button">取消</button></div>
            </section>
          </div>
        )
      })()}
      {error && <p role="alert" className="runtime-error workbench-inline-message">{error}</p>}
      {message && <p role="status" className="runtime-message workbench-inline-message">{message}</p>}
    </div>
  )
}

function MetricCard({ slot, onOpen }: { slot: WorkbenchSlotView; onOpen: () => void }) {
  const content = (
    <>
      <span className="workbench-metric__icon" aria-hidden="true">{SLOT_ICONS[slot.id]}</span>
      <span className="workbench-metric__body">
        <span className="workbench-metric__label">{slot.label}</span>
        <span className="workbench-metric__reading"><strong>{slot.reading.valueText}</strong><small>{slot.reading.unit}</small></span>
        <span className="workbench-metric__source">{slot.entity?.node_name || slot.bindingLabel}</span>
      </span>
      <span className={`workbench-metric__state is-${slot.reading.kind}`}>{slot.reading.kind === 'current' ? '当前值' : slot.reading.kind === 'last' ? '最后值（非当前）' : slot.entity ? '当前未知' : slot.bindingLabel}</span>
      <span className="workbench-metric__reason">{slot.reading.reason || slot.reason}</span>
    </>
  )
  return slot.entity ? (
    <button type="button" data-workbench-slot={slot.id} onClick={onOpen} className="workbench-metric neu-card">{content}</button>
  ) : <article data-workbench-slot={slot.id} className="workbench-metric neu-card">{content}</article>
}

function FlowNode({ slot, role }: { slot: WorkbenchSlotView; role: string }) {
  const stateLabel = slot.reading.kind === 'current' ? '当前值' : slot.reading.kind === 'last' ? '最后值（非当前）' : '当前未知'
  return (
    <div className={`workbench-flow-node workbench-flow-node--${role}`}>
      <span aria-hidden="true">{SLOT_ICONS[slot.id]}</span>
      <div>
        <strong>{slot.label}</strong>
        <p>{slot.reading.valueText} <small>{slot.reading.unit}</small></p>
        <em>{slot.entity?.node_name || slot.bindingLabel}</em>
        <small className={`workbench-flow-node__state is-${slot.reading.kind}`}>{stateLabel} · 质量{qualityLabel(slot.reading.quality)} · 最后值 {formatTime(slot.reading.observedAt)}</small>
        <small className="workbench-flow-node__reason">{slot.reading.reason || slot.reason}</small>
      </div>
    </div>
  )
}

function SlotConfigurationDialog({
  slots,
  descriptors,
  configurationRevision,
  directoryError,
  onSaved,
  onClose,
}: {
  slots: WorkbenchSlotView[]
  descriptors: EntityInstance[]
  configurationRevision: number
  directoryError: string
  onSaved: () => Promise<EmsWorkbench>
  onClose: () => void
}) {
  const [selections, setSelections] = useState<Record<string, string>>(() => Object.fromEntries(slots.map((slot) => [slot.id, slot.entity?.entity_instance_id || ''])))
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const revisionRef = useRef(configurationRevision)
  const keys = useRef(new Map<string, string>())
  const candidates = (slot: WorkbenchSlotView) => {
    const compatibleIds = new Set(compatibleSlotEntities(slot.id, descriptors).map((entity) => entity.id))
    return descriptors.filter((entity) => compatibleIds.has(entity.id))
  }
  const save = async (slot: WorkbenchSlotView, value: string | null) => {
    if (busy) return
    const entityId = value || null
    const identity = `${slot.id}\u0000${entityId || 'auto'}\u0000${revisionRef.current}`
    const key = keys.current.get(identity) || crypto.randomUUID()
    keys.current.set(identity, key)
    setBusy(slot.id)
    setMessage('')
    setError('')
    try {
      const receipt = await saveEmsWorkbenchSlot(slot.id, entityId, revisionRef.current, key)
      revisionRef.current = receipt.configuration_revision
      try {
        const refreshed = await onSaved()
        revisionRef.current = refreshed.configuration_revision
        const refreshedSlots = buildWorkbenchSlots(refreshed.kpis)
        setSelections(Object.fromEntries(refreshedSlots.map((item) => [item.id, item.entity?.entity_instance_id || ''])))
        setMessage(`${slot.label}已保存 · 配置修订 ${refreshed.configuration_revision}${receipt.replayed ? ' · 幂等重放' : ''}`)
      } catch (reason) {
        setError(`${slot.label}保存已受理（配置修订 ${receipt.configuration_revision}），但读取最新工作台失败：${controlError(reason, '请重试读取；当前页面未把本地选择当作正式绑定。')}`)
      }
    } catch (reason) {
      setError(`${slot.label}保存失败：${describeWorkbenchSlotError(reason)}`)
    } finally {
      setBusy(null)
    }
  }
  return (
    <div className="runtime-detail-backdrop" role="presentation">
      <section role="dialog" aria-modal="true" aria-label="配置首页指标" className="workbench-slot-dialog neu-card">
        <header><div><p className="runtime-eyebrow">固定 EMS 工作台</p><h3>配置首页指标</h3><span>当前配置修订 {revisionRef.current} · 只列类型与单位兼容的真实 L2</span></div><button type="button" onClick={onClose} className="neu-btn runtime-touch-button">关闭</button></header>
        <div className="workbench-slot-dialog__list">
          {slots.map((slot) => (
            <article key={slot.id}>
              <div><strong>{slot.label}</strong><span>{slot.bindingLabel} · {slot.reason}</span></div>
              <select aria-label={`${slot.label}绑定`} value={selections[slot.id]} onChange={(event) => setSelections((current) => ({ ...current, [slot.id]: event.target.value }))} className="neu-input">
                <option value="">自动匹配</option>
                {candidates(slot).map((entity) => <option key={entity.id} value={entity.id}>{entity.node_display_name} · {entity.display_name} · {entity.unit}</option>)}
              </select>
              <button type="button" disabled={busy !== null} onClick={() => void save(slot, selections[slot.id])} className="zizu-primary runtime-touch-button">保存{slot.label}</button>
              <button type="button" disabled={busy !== null} onClick={() => void save(slot, null)} className="neu-btn runtime-touch-button">清除{slot.label}绑定</button>
            </article>
          ))}
        </div>
        {directoryError && <p role="alert" className="runtime-error workbench-inline-message">L2 目录不可用：{directoryError}。现有绑定仍可清除，但不能选择新实体。</p>}
        {message && <p role="status" className="runtime-message workbench-inline-message">{message}</p>}
        {error && <p role="alert" className="runtime-error workbench-inline-message">{error}</p>}
      </section>
    </div>
  )
}

export default function EMSWorkbenchPage({
  onOpenAlarms,
  onOpenEngineering,
  onOpenDevices,
  initialTab = 'overview',
}: RuntimeProps) {
  const [workbench, setWorkbench] = useState<EmsWorkbench | null>(null)
  const [workbenchError, setWorkbenchError] = useState('')
  const [workbenchLoading, setWorkbenchLoading] = useState(true)
  const [descriptors, setDescriptors] = useState<EntityInstance[]>([])
  const [directoryError, setDirectoryError] = useState('')
  const [alarms, setAlarms] = useState<Alarm[]>([])
  const [alarmTotal, setAlarmTotal] = useState<number | null>(null)
  const [alarmError, setAlarmError] = useState('')
  const [strategies, setStrategies] = useState<DispatchStrategy[]>([])
  const [strategyError, setStrategyError] = useState('')
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null)
  const [configuring, setConfiguring] = useState(false)
  const generation = useRef(0)

  const load = useCallback(() => {
    const current = ++generation.current
    setWorkbenchLoading(true)
    setWorkbenchError('')
    void Promise.allSettled([
      fetchEmsWorkbench(),
      fetchEntityInstances(),
      fetchAlarms(1, 10, undefined, undefined, false, false),
      fetchDispatchStrategies(),
    ]).then(([workbenchResult, directoryResult, alarmResult, strategyResult]) => {
      if (current !== generation.current) return
      if (workbenchResult.status === 'fulfilled') setWorkbench(workbenchResult.value)
      else setWorkbenchError(controlError(workbenchResult.reason, '读取 EMS 工作台失败。'))
      if (directoryResult.status === 'fulfilled') { setDescriptors(directoryResult.value.items); setDirectoryError('') }
      else setDirectoryError(controlError(directoryResult.reason, '读取 L2 实体目录失败。'))
      if (alarmResult.status === 'fulfilled') { setAlarms(alarmResult.value.alarms); setAlarmTotal(alarmResult.value.total); setAlarmError('') }
      else setAlarmError(controlError(alarmResult.reason, '读取待处理告警失败。'))
      if (strategyResult.status === 'fulfilled') { setStrategies(strategyResult.value); setStrategyError('') }
      else setStrategyError(controlError(strategyResult.reason, '读取调度摘要失败。'))
    }).finally(() => { if (current === generation.current) setWorkbenchLoading(false) })
  }, [])

  const reloadWorkbench = useCallback(async (): Promise<EmsWorkbench> => {
    setWorkbenchLoading(true)
    setWorkbenchError('')
    try {
      const latest = await fetchEmsWorkbench()
      setWorkbench(latest)
      return latest
    } catch (reason) {
      setWorkbenchError(controlError(reason, '读取 EMS 工作台失败。'))
      throw reason
    } finally {
      setWorkbenchLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    return () => { generation.current += 1 }
  }, [load])

  const { states } = useRuntimeNodes(descriptors)
  const projections = useMemo(() => new Map(
    [...states.entries()].flatMap(([nodeId, state]) => state.projection ? [[nodeId, state.projection] as const] : []),
  ), [states])
  const runtimeEntities = useMemo(() => buildRuntimeNodes(descriptors, projections).flatMap((node) => node.entities), [descriptors, projections])
  const selected: RuntimeEntity | null = runtimeEntities.find((entity) => entity.descriptor.id === selectedEntityId) || null
  const runtimeEvidence = useMemo<WorkbenchRuntimeEvidence[]>(() => runtimeEntities.map(({ descriptor, observation }) => {
    const state = states.get(descriptor.node_id)
    return {
      entityInstanceId: descriptor.id,
      observation,
      nodeCurrent: state?.status === 'current' && state.projection?.status === 'COMPLETE',
      reason: state?.error || (state?.status === 'loading' ? '正在读取已提交实时帧。' : undefined),
    }
  }), [runtimeEntities, states])
  const slots = useMemo(() => buildWorkbenchSlots(workbench?.kpis || [], runtimeEvidence, !workbenchError), [runtimeEvidence, workbench, workbenchError])
  const flow = useMemo(() => fixedEnergyFlow(slots), [slots])
  const enabledStrategies = strategies.filter((strategy) => strategy.enabled)
  const primaryStrategy = enabledStrategies[0] || strategies[0] || null
  const controls = workbench ? mergeControlEvidence(workbench) : []

  if (initialTab === 'controls') {
    return (
      <section className="runtime-shell workbench-page workbench-control-page">
        <header className="workbench-page-heading"><div><p className="runtime-eyebrow">统一安全入口</p><h2>手动控制</h2><span>目标输入 → 二次确认 → 正式下发 → committed L2 回读</span></div><button type="button" onClick={load} disabled={workbenchLoading} className="neu-btn runtime-touch-button">{workbenchLoading ? '刷新中…' : '刷新控制目录'}</button></header>
        {workbenchError && <div role="alert" className="runtime-error neu-card"><span>{workbenchError}。现有内容不按空目录处理。</span><button type="button" onClick={load}>重试</button></div>}
        {!workbench && !workbenchError ? <div className="runtime-empty neu-card">正在读取正式控制目录…</div> : null}
        {workbench ? (workbench.controls.visible ? <Controls entities={controls} /> : <div className="runtime-empty neu-card">当前角色或配置没有可用控制。</div>) : null}
      </section>
    )
  }

  if (!workbench) {
    return (
      <section className="runtime-shell workbench-page">
        <header className="workbench-page-heading">
          <div><p className="runtime-eyebrow">已提交 L2 · 固定 EMS 工作台</p><h2>运行总览</h2><span>真实值、质量与时间来自同一正式接口；异常状态不补零。</span></div>
          <button type="button" onClick={load} disabled={workbenchLoading} className="neu-btn runtime-touch-button">{workbenchLoading ? '刷新中…' : '重试'}</button>
        </header>
        {workbenchError ? <div role="alert" className="runtime-error neu-card"><span>运行首页读取失败：{workbenchError}。失败不按空站或零值处理。</span><button type="button" onClick={load}>重试</button></div> : <div role="status" className="runtime-empty neu-card">正在读取固定首页槽位、告警与调度摘要…</div>}
      </section>
    )
  }

  return (
    <section className="runtime-shell workbench-page">
      <header className="workbench-page-heading">
        <div><p className="runtime-eyebrow">已提交 L2 · 固定 EMS 工作台</p><h2>运行总览</h2><span>真实值、质量与时间来自同一正式接口；异常状态不补零。</span></div>
        <div>{onOpenEngineering && <button type="button" onClick={() => setConfiguring(true)} className="neu-btn runtime-touch-button">配置首页指标</button>}<button type="button" onClick={load} disabled={workbenchLoading} className="neu-btn runtime-touch-button">{workbenchLoading ? '刷新中…' : '刷新'}</button></div>
      </header>
      {workbenchError && <div role="alert" className="runtime-error neu-card"><span>运行首页读取失败：{workbenchError}。旧内容不按零处理。</span><button type="button" onClick={load}>重试</button></div>}
      <div className="workbench-metrics" aria-label="关键指标">
        {slots.map((slot) => <MetricCard key={slot.id} slot={slot} onOpen={() => setSelectedEntityId(slot.entity?.entity_instance_id || null)} />)}
      </div>
      <div className="workbench-dashboard">
        <section className="workbench-flow neu-card" role="region" aria-label="站点能流">
          <header><div><h3>站点能流</h3><p>固定光伏—储能—充电—电网/负荷拓扑</p></div><span className="workbench-health">● {slots.some((slot) => slot.reading.kind === 'current') ? '已取得当前数据' : '暂无当前数据'}</span></header>
          <div className="workbench-flow__canvas">
            <FlowNode slot={slots[1]} role="pv" />
            <FlowNode slot={{ ...slots[0], label: '电网 / 负荷' }} role="site" />
            <div className="workbench-flow__bus" aria-hidden="true"><i /><i /><i /><i /></div>
            <FlowNode slot={slots[2]} role="storage" />
            <FlowNode slot={slots[4]} role="charging" />
          </div>
          <footer><span>ⓘ {flow.reason}</span>{onOpenDevices && <button type="button" onClick={onOpenDevices}>查看设备 →</button>}</footer>
        </section>
        <aside className="workbench-side">
          <section className="workbench-summary neu-card" role="region" aria-label="待处理告警">
            <header><h3>待处理告警</h3><strong>{alarmTotal == null ? '—' : alarmTotal}<small> 条</small></strong></header>
            {alarmError ? <p className="workbench-summary__error">{alarmError}</p> : alarms.length ? <div className="workbench-alarm-list">{alarms.slice(0, 3).map((alarm) => <div key={alarm.id}><span>♧</span><p><strong>{alarm.message}</strong><small>{alarm.node_name || alarm.entity_name || '来源未记录'} · {formatTime(alarm.created_at)}</small></p></div>)}</div> : <p className="workbench-summary__empty">当前无待处理告警</p>}
            <button type="button" onClick={onOpenAlarms} className="neu-btn runtime-touch-button">查看告警 →</button>
          </section>
          <section className="workbench-summary workbench-dispatch neu-card" role="region" aria-label="调度摘要">
            <header><h3>调度摘要</h3><span>{primaryStrategy ? (primaryStrategy.enabled ? '运行中' : '未启用') : '未配置'}</span></header>
            {strategyError ? <p className="workbench-summary__error">{strategyError}</p> : primaryStrategy ? <dl><div><dt>当前策略</dt><dd>{primaryStrategy.name}</dd></div><div><dt>运行状态</dt><dd>{primaryStrategy.runtime_health}</dd></div><div><dt>最近求值</dt><dd>{formatTime(primaryStrategy.last_evaluated_at)}</dd></div><div><dt>已启用策略</dt><dd>{enabledStrategies.length}</dd></div></dl> : <p className="workbench-summary__empty">暂无真实调度策略</p>}
            {onOpenEngineering ? <button type="button" onClick={() => onOpenEngineering()} className="neu-btn runtime-touch-button">前往工程配置 →</button> : <p className="workbench-summary__note">策略配置仅对实施工程师和管理员开放。</p>}
          </section>
        </aside>
      </div>
      {directoryError && <p role="status" className="workbench-detail-notice">L2 详情目录暂不可用：{directoryError}。已绑定槽位只保留最后值，不标记为当前。</p>}
      {configuring && <SlotConfigurationDialog slots={slots} descriptors={descriptors} configurationRevision={workbench.configuration_revision} directoryError={directoryError} onSaved={reloadWorkbench} onClose={() => setConfiguring(false)} />}
      {selected && <EntityRuntimeDetail descriptor={selected.descriptor} observation={selected.observation} l0={[...(states.get(selected.descriptor.node_id)?.projection?.l0.values() || [])]} nodeCurrent={states.get(selected.descriptor.node_id)?.status === 'current' && states.get(selected.descriptor.node_id)?.projection?.status === 'COMPLETE'} onClose={() => setSelectedEntityId(null)} />}
    </section>
  )
}
