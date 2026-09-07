import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchAlarmCounts,
  fetchControlCommand,
  fetchEmsWorkbench,
  fetchEntityInstances,
  reconcileControlCommand,
  requestControlConfirmation,
  submitControlCommand,
  type ControlCommand,
  type ControlConfirmation,
  type EmsWorkbench,
  type EntityInstance,
  type WorkbenchEntity,
} from '../api/client'
import EntityRuntimeDetail from '../components/runtime-monitoring/EntityRuntimeDetail'
import {
  buildRuntimeNodes,
  runtimeEntityReading,
  type RuntimeEntity,
} from '../components/runtime-monitoring/runtimeModel'
import { useRuntimeNodes } from '../components/runtime-monitoring/useRuntimeNodes'
import '../components/runtime-monitoring/runtime-monitoring.css'

export type RuntimeTab = 'overview' | 'trends' | 'alarms' | 'controls'

export type RuntimeProps = {
  onOpenAlarms: () => void
  onOpenEngineering?: (nodeId?: string) => void
  onOpenDevices?: () => void
  initialTab?: RuntimeTab
}

function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未记录'
}

function formatValue(value: unknown): string {
  if (Array.isArray(value)) return value.join('、')
  return value == null ? '—' : String(value)
}

function qualityLabel(quality: number | null): string {
  if (quality === 192) return '正常'
  if (quality === 64) return '超时'
  if (quality === 1) return '未知'
  if (quality == null) return '无数据'
  return '异常'
}

function Controls({ entities }: { entities: WorkbenchEntity[] }) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [confirmation, setConfirmation] = useState<{
    entity: WorkbenchEntity
    value: unknown
    receipt: ControlConfirmation
  } | null>(null)
  const [commands, setCommands] = useState<Record<string, ControlCommand>>({})
  const [message, setMessage] = useState('')
  const valueFor = (entity: WorkbenchEntity): unknown | null => {
    const raw = values[entity.entity_instance_id]
    if (raw == null || raw === '') {
      setMessage(`请先填写 ${entity.display_name} 的目标值。`)
      return null
    }
    const value = entity.data_type === 'bool'
      ? raw === 'true'
      : ['float', 'int'].includes(entity.data_type) ? Number(raw) : raw
    if (typeof value === 'number' && !Number.isFinite(value)) {
      setMessage('目标值必须是有效数字。')
      return null
    }
    return value
  }
  const prepare = async (entity: WorkbenchEntity) => {
    const value = valueFor(entity)
    if (value === null) return
    try {
      const receipt = await requestControlConfirmation(entity.entity_instance_id, value)
      setConfirmation({ entity, value, receipt })
      setMessage(`请核对目标值后在 60 秒内确认下发：${entity.display_name} = ${String(value)}${entity.unit ? ` ${entity.unit}` : ''}。`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '控制二次确认申请失败。')
    }
  }
  const execute = async () => {
    if (!confirmation) return
    try {
      const command = await submitControlCommand(
        confirmation.entity.entity_instance_id,
        confirmation.value,
        confirmation.receipt.id,
      )
      setCommands((previous) => ({ ...previous, [confirmation.entity.entity_instance_id]: command }))
      setConfirmation(null)
      setMessage(`命令已受理：${command.id}（${command.status}）。请等待设备回读后刷新状态。`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '控制命令提交失败。')
    }
  }
  const refresh = async (entityInstanceId: string, command: ControlCommand) => {
    try {
      const reconciled = await reconcileControlCommand(command.id)
      const current = reconciled.status === 'dispatched' ? await fetchControlCommand(command.id) : reconciled
      setCommands((previous) => ({ ...previous, [entityInstanceId]: current }))
      setMessage(`命令 ${current.id} 当前状态：${current.status}（${current.code}）。`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '命令状态刷新失败。')
    }
  }
  if (entities.length === 0) return <p className="runtime-empty">当前没有可授权控制的 L2 全局实体。</p>
  return (
    <div className="runtime-controls">
      {entities.map((entity) => (
        <div key={entity.entity_instance_id} className="runtime-control-row neu-inset">
          <div>
            <strong>{entity.display_name}</strong>
            <span>{entity.node_name} · {entity.definition_id}</span>
          </div>
          {entity.data_type === 'bool' ? (
            <select aria-label={`${entity.display_name}目标值`} value={values[entity.entity_instance_id] || ''} onChange={(event) => setValues({ ...values, [entity.entity_instance_id]: event.target.value })} className="neu-input">
              <option value="">选择</option><option value="true">开启</option><option value="false">关闭</option>
            </select>
          ) : (
            <input aria-label={`${entity.display_name}目标值`} value={values[entity.entity_instance_id] || ''} onChange={(event) => setValues({ ...values, [entity.entity_instance_id]: event.target.value })} type={['float', 'int'].includes(entity.data_type) ? 'number' : 'text'} className="neu-input" placeholder={entity.unit || '目标值'} />
          )}
          <button type="button" onClick={() => void prepare(entity)} className="zizu-primary runtime-touch-button">核对目标</button>
          {commands[entity.entity_instance_id] && <button type="button" onClick={() => void refresh(entity.entity_instance_id, commands[entity.entity_instance_id])} className="neu-btn runtime-touch-button">刷新回读</button>}
        </div>
      ))}
      {confirmation && (
        <div role="alertdialog" aria-label="确认控制命令" className="runtime-confirmation">
          <strong>请确认控制目标</strong>
          <p>{confirmation.entity.display_name} = {String(confirmation.value)}{confirmation.entity.unit ? ` ${confirmation.entity.unit}` : ''}</p>
          <p>二次确认仅在 {new Date(confirmation.receipt.expires_at).toLocaleTimeString()} 前有效。提交后仍需等待设备回读确认。</p>
          <div><button type="button" onClick={() => void execute()} className="runtime-danger runtime-touch-button">确认下发</button><button type="button" onClick={() => setConfirmation(null)} className="neu-btn runtime-touch-button">取消</button></div>
        </div>
      )}
      {message && <p role="status" className="runtime-message">{message}</p>}
    </div>
  )
}

function RuntimeMetric({
  entity,
  nodeCurrent,
  onOpen,
}: {
  entity: RuntimeEntity
  nodeCurrent: boolean
  onOpen: () => void
}) {
  const reading = runtimeEntityReading(entity.observation, nodeCurrent)
  const quality = qualityLabel(reading.quality)
  return (
    <button type="button" className="runtime-metric neu-inset" onClick={onOpen}>
      <span className="runtime-metric__identity">
        <strong>{entity.descriptor.display_name}</strong>
        <small>{entity.descriptor.definition_id}</small>
      </span>
      <span className="runtime-metric__value font-mono-value">
        {formatValue(reading.value)}{entity.descriptor.unit ? <small> {entity.descriptor.unit}</small> : null}
      </span>
      <span className={`runtime-quality runtime-quality--${reading.quality ?? 'unknown'}`}>{quality}</span>
      <span className="runtime-metric__evidence">
        {reading.kind === 'current' ? '当前值' : reading.kind === 'last' ? '最后值（非当前）' : '无采样'} · {formatTime(reading.kind === 'last' ? reading.valueObservedAt || reading.observedAt : reading.observedAt)}
      </span>
    </button>
  )
}

export default function EMSWorkbenchPage({
  onOpenAlarms,
  onOpenEngineering,
  onOpenDevices,
  initialTab = 'overview',
}: RuntimeProps) {
  const [activeTab, setActiveTab] = useState<RuntimeTab>(initialTab)
  const [descriptors, setDescriptors] = useState<EntityInstance[]>([])
  const [directoryLoading, setDirectoryLoading] = useState(true)
  const [directoryLoaded, setDirectoryLoaded] = useState(false)
  const [directoryError, setDirectoryError] = useState('')
  const [alarmCounts, setAlarmCounts] = useState<Record<string, number>>(Object.create(null))
  const [alarmCountsError, setAlarmCountsError] = useState('')
  const [alarmCountsLoading, setAlarmCountsLoading] = useState(true)
  const [workbench, setWorkbench] = useState<EmsWorkbench | null>(null)
  const [workbenchError, setWorkbenchError] = useState('')
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null)
  const directoryGeneration = useRef(0)
  const alarmGeneration = useRef(0)
  const workbenchGeneration = useRef(0)

  useEffect(() => setActiveTab(initialTab), [initialTab])

  const loadDirectory = useCallback(() => {
    const generation = ++directoryGeneration.current
    setDirectoryLoading(true)
    setDirectoryError('')
    void fetchEntityInstances().then((payload) => {
      if (generation !== directoryGeneration.current) return
      setDescriptors(payload.items)
      setDirectoryLoaded(true)
    }).catch((reason) => {
      if (generation !== directoryGeneration.current) return
      setDirectoryError(reason instanceof Error ? reason.message : '读取 L2 实体目录失败。')
    }).finally(() => {
      if (generation === directoryGeneration.current) setDirectoryLoading(false)
    })
  }, [])

  const loadAlarmCounts = useCallback(() => {
    const generation = ++alarmGeneration.current
    setAlarmCountsLoading(true)
    setAlarmCountsError('')
    void fetchAlarmCounts().then((counts) => {
      if (generation === alarmGeneration.current) setAlarmCounts(counts)
    }).catch((reason) => {
      if (generation !== alarmGeneration.current) return
      setAlarmCountsError(reason instanceof Error ? reason.message : '读取告警计数失败。')
    }).finally(() => {
      if (generation === alarmGeneration.current) setAlarmCountsLoading(false)
    })
  }, [])

  const loadWorkbench = useCallback(() => {
    const generation = ++workbenchGeneration.current
    setWorkbenchError('')
    void fetchEmsWorkbench().then((payload) => {
      if (generation === workbenchGeneration.current) setWorkbench(payload)
    }).catch((reason) => {
      if (generation !== workbenchGeneration.current) return
      setWorkbenchError(reason instanceof Error ? reason.message : '读取控制目录失败。')
    })
  }, [])

  useEffect(() => {
    loadDirectory()
    loadAlarmCounts()
    loadWorkbench()
    return () => {
      directoryGeneration.current += 1
      alarmGeneration.current += 1
      workbenchGeneration.current += 1
    }
  }, [loadAlarmCounts, loadDirectory, loadWorkbench])

  const { states, retryNode } = useRuntimeNodes(descriptors)
  const projections = useMemo(() => new Map(
    [...states.entries()].flatMap(([nodeId, state]) => state.projection ? [[nodeId, state.projection] as const] : []),
  ), [states])
  const nodes = useMemo(() => buildRuntimeNodes(descriptors, projections), [descriptors, projections])
  const allEntities = useMemo(() => nodes.flatMap((node) => node.entities), [nodes])
  const selected = allEntities.find((entity) => entity.descriptor.id === selectedEntityId) || null

  const changeTab = (tab: RuntimeTab) => {
    if (tab === 'alarms') onOpenAlarms()
    else setActiveTab(tab)
  }

  return (
    <section className="runtime-shell">
      <header className="runtime-hero neu-card">
        <div>
          <p className="runtime-eyebrow">自足IOT · 已提交 L2</p>
          <h2>光储充现场</h2>
          <p>按真实节点与实体实例展示，不以首台设备代替全站。</p>
        </div>
        <div className="runtime-hero__actions">
          {onOpenDevices && <button type="button" onClick={onOpenDevices} className="neu-btn runtime-touch-button">设备监控</button>}
          {onOpenEngineering && <button type="button" onClick={() => onOpenEngineering()} className="neu-btn runtime-touch-button">工程配置</button>}
          <button type="button" onClick={loadDirectory} className="neu-btn runtime-touch-button" disabled={directoryLoading}>刷新目录</button>
        </div>
      </header>

      <nav className="runtime-tabs neu-card" aria-label="运行工作台">
        {([['overview', '概览'], ['trends', '历史'], ['alarms', '告警'], ['controls', '控制']] as Array<[RuntimeTab, string]>).map(([tab, label]) => (
          <button type="button" key={tab} onClick={() => changeTab(tab)} className={activeTab === tab ? 'zizu-tab-active' : 'neu-btn'}>{label}</button>
        ))}
      </nav>

      {directoryError && (
        <div role="alert" className="runtime-error neu-card">
          <span>实体目录读取失败：{directoryError}。现有内容不按零处理。</span>
          <button type="button" onClick={loadDirectory}>重试目录</button>
        </div>
      )}

      {activeTab === 'overview' && (
        <div className="runtime-content">
          {directoryLoading && !directoryLoaded ? <div className="runtime-empty neu-card">正在读取真实 L2 实体目录…</div> : null}
          {directoryLoaded && descriptors.length === 0 ? (
            <div className="runtime-unconfigured neu-card">
              <strong>运行首页未配置</strong>
              <p>当前没有已确认的 L2 全局实体。请由实施工程师沿节点 → L0 → L1 → L2 完成配置；本站不生成推测总功率或能流。</p>
              {onOpenEngineering && <button type="button" onClick={() => onOpenEngineering()} className="zizu-primary runtime-touch-button">前往工程配置</button>}
            </div>
          ) : null}
          <div className="runtime-node-grid">
            {nodes.map((node) => {
              const state = states.get(node.nodeId)
              const current = state?.status === 'current' && state.projection?.status === 'COMPLETE'
              const alarmCount = alarmCountsError ? null : alarmCounts[node.nodeId] ?? 0
              return (
                <article key={node.nodeId} aria-label={`${node.nodeName} 运行数据`} className="runtime-node neu-card">
                  <header>
                    <div><p>{node.nodeType || '其他'}</p><h3>{node.nodeName}</h3><span>{node.nodeId}</span></div>
                    <div className="runtime-node__actions">
                      <button type="button" onClick={onOpenAlarms} className="runtime-alarm-count" aria-label={`${node.nodeName}未恢复告警`}>
                        未恢复 {alarmCountsLoading ? '…' : alarmCount == null ? '—' : alarmCount}
                      </button>
                      {onOpenEngineering && <button type="button" onClick={() => onOpenEngineering(node.nodeId)} className="neu-btn">配置此节点</button>}
                    </div>
                  </header>
                  {alarmCountsError && <p className="runtime-node__notice">告警计数未知 · <button type="button" onClick={loadAlarmCounts}>重试</button></p>}
                  {state?.error && <p className="runtime-node__notice">实时链路：{state.error} · <button type="button" onClick={() => retryNode(node.nodeId)}>重试</button></p>}
                  <div className="runtime-node__metrics">
                    {node.entities.map((entity) => <RuntimeMetric key={entity.descriptor.id} entity={entity} nodeCurrent={current} onOpen={() => setSelectedEntityId(entity.descriptor.id)} />)}
                  </div>
                  <footer>
                    <span>节点帧 {state?.projection?.frameSequence ?? '未记录'}</span>
                    <span>配置修订 {state?.projection?.configurationRevision ?? workbench?.configuration_revision ?? '未记录'}</span>
                    <span>{state?.status === 'current' ? '实时连接' : state?.status === 'loading' ? '正在重验' : '非当前'}</span>
                  </footer>
                </article>
              )
            })}
          </div>
        </div>
      )}

      {activeTab === 'trends' && (
        <section className="runtime-history-index neu-card">
          <header><div><p className="runtime-eyebrow">按实体、按单位</p><h3>历史与来源</h3></div><span>选择一个实体后才读取历史，不混合节点或单位。</span></header>
          {allEntities.length === 0 ? <div className="runtime-empty">暂无可查询实体。</div> : (
            <div className="runtime-history-index__list">
              {allEntities.map((entity) => (
                <button type="button" key={entity.descriptor.id} onClick={() => setSelectedEntityId(entity.descriptor.id)} className="neu-inset">
                  <span><strong>{entity.descriptor.display_name}</strong><small>{entity.descriptor.node_display_name} · {entity.descriptor.definition_id}</small></span>
                  <span>{entity.descriptor.unit || '无单位'}</span>
                </button>
              ))}
            </div>
          )}
        </section>
      )}

      {activeTab === 'controls' && (
        <section className="runtime-control-panel neu-card">
          <header><div><p className="runtime-eyebrow">统一安全入口</p><h3>授权控制</h3></div><span>受理不等于设备成功，必须等待回读。</span></header>
          {workbenchError && <div className="runtime-error"><span>{workbenchError}</span><button type="button" onClick={loadWorkbench}>重试控制目录</button></div>}
          {workbench ? (workbench.controls.visible ? <Controls entities={workbench.controls.entities} /> : <p className="runtime-empty">当前角色或配置没有可用控制。</p>) : !workbenchError ? <p className="runtime-empty">正在读取控制目录…</p> : null}
        </section>
      )}

      {selected && (
        <EntityRuntimeDetail
          descriptor={selected.descriptor}
          observation={selected.observation}
          l0={[...(states.get(selected.descriptor.node_id)?.projection?.l0.values() || [])]}
          nodeCurrent={states.get(selected.descriptor.node_id)?.status === 'current' && states.get(selected.descriptor.node_id)?.projection?.status === 'COMPLETE'}
          onClose={() => setSelectedEntityId(null)}
        />
      )}
    </section>
  )
}
