import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import '../components/alarm-center/tabletApplications.css'
import {
  clearDispatchStrategyFailure,
  createDispatchStrategy,
  disableDispatchStrategy,
  enableDispatchStrategy,
  fetchControlCommand,
  fetchDispatchStrategies,
  fetchDispatchStrategy,
  fetchDispatchStrategyEvents,
  fetchEntityInstanceRealtime,
  fetchEntityInstances,
  publishDispatchStrategy,
  saveDispatchStrategyDraft,
  simulateDispatchStrategy,
  type DispatchStrategy,
  type DispatchStrategyEvent,
  type DispatchStrategySimulation,
  type DispatchStrategyBinding,
  type EntityInstance,
  type EntityInstanceObservation,
} from '../api/client'
import {
  createDispatchLoadGate,
  describeDispatchStrategyError,
  dispatchStrategyFailureState,
  isJdmGraphUnchanged,
  makeStrategyBinding,
  projectStrategyStatus,
  retainDispatchReloadLock,
} from '../components/dispatch-strategy/dispatchStrategyModel.mjs'
import {
  addNativeExampleColumns,
  buildGenericDecisionTableJdm,
  bindingsForDraft,
  inspectNativeDecisionTable,
  isNativeDecisionInputEntity,
  isNativeDecisionOutputEntity,
  mergeNativeDecisionGraph,
  replaceDecisionTableContent,
  updateStrategyBinding,
  validateBindingAliases,
} from '../components/dispatch-strategy/nativeDecisionTableModel'

const NativeDecisionTableEditor = lazy(() => import('../components/dispatch-strategy/NativeDecisionTableEditor'))
const NativeDecisionGraphEditor = lazy(() => import('../components/dispatch-strategy/NativeDecisionTableEditor').then((module) => ({ default: module.NativeDecisionGraphEditor })))

type DecisionGraphType = { nodes: any[]; edges: any[]; [key: string]: any }

const HEALTH_STYLES: Record<string, string> = {
  READY: 'bg-green-100 text-green-700',
  IDLE: 'bg-gray-100 text-gray-600',
  BLOCKED: 'bg-amber-100 text-amber-700',
  FAILED: 'bg-red-100 text-red-700',
}

function valueText(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function qualityText(observation: EntityInstanceObservation | null | undefined): string {
  if (!observation) return '尚未读取'
  if (!observation.fresh) return '超时'
  return observation.quality_good ? '正常' : `异常(${observation.quality})`
}

export default function DispatchStrategyPage() {
  const [strategies, setStrategies] = useState<DispatchStrategy[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [strategy, setStrategy] = useState<DispatchStrategy | null>(null)
  const [entities, setEntities] = useState<EntityInstance[]>([])
  const [observations, setObservations] = useState<Record<string, EntityInstanceObservation | null>>({})
  const [events, setEvents] = useState<DispatchStrategyEvent[]>([])
  const [eventPageSize, setEventPageSize] = useState(10)
  const [eventCursor, setEventCursor] = useState<string | null>(null)
  const [nextEventCursor, setNextEventCursor] = useState<string | null>(null)
  const [eventHistory, setEventHistory] = useState<(string | null)[]>([])
  const [eventsBusy, setEventsBusy] = useState(false)
  const [eventsError, setEventsError] = useState('')
  const eventGeneration = useRef(0)
  const [controlEvidence, setControlEvidence] = useState<{ id: string; command?: Awaited<ReturnType<typeof fetchControlCommand>>; error?: string } | null>(null)
  const [name, setName] = useState('')
  const [triggerKind, setTriggerKind] = useState<'DATA_CHANGE' | 'FIXED_TICK'>('DATA_CHANGE')
  const [draftBindings, setDraftBindings] = useState<DispatchStrategyBinding[]>([])
  const [bindingsEdited, setBindingsEdited] = useState(false)
  const [graph, setGraph] = useState<DecisionGraphType>(() => buildGenericDecisionTableJdm() as DecisionGraphType)
  const [showGraph, setShowGraph] = useState(false)
  const [editorPending, setEditorPending] = useState(false)
  const editorPendingRef = useRef(false)
  const [editorSession, setEditorSession] = useState(0)
  const [simulation, setSimulation] = useState<DispatchStrategySimulation | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [draftReceipt, setDraftReceipt] = useState<{ id: string; digest: string; revision: number } | null>(null)
  const [requiresReload, setRequiresReload] = useState(false)
  const [reloadNonce, setReloadNonce] = useState(0)
  const loadGate = useRef(createDispatchLoadGate())
  const editGeneration = useRef(0)

  const currentRevision = strategy?.draft || strategy?.published_revision || strategy?.active_revision || null
  const nativeTable = useMemo(() => inspectNativeDecisionTable(graph), [graph])
  const editorGraph = useMemo(() => {
    const view = structuredClone(graph)
    view.nodes = view.nodes.map((node, index) => node.position ? node : { ...node, position: { x: index * 320, y: 80 } })
    return view
  }, [graph])
  const status = strategy ? projectStrategyStatus(strategy) : null

  const inputEntities = useMemo(() => entities.filter(isNativeDecisionInputEntity), [entities])
  const outputEntities = useMemo(() => entities.filter(isNativeDecisionOutputEntity), [entities])
  const dirty = !!strategy && !!currentRevision && (editorPending || name.trim() !== strategy.name
    || triggerKind !== currentRevision.trigger_kind
    || !isJdmGraphUnchanged(graph, currentRevision.jdm_content)
    || !isJdmGraphUnchanged(draftBindings, currentRevision.bindings))

  const selectedSummaryOutputId = useMemo(() => {
    const revision = strategy?.active_revision || strategy?.published_revision || strategy?.draft
    return revision?.bindings
      .filter((item) => item.direction === 'OUTPUT')
      .sort((left, right) => left.ordinal - right.ordinal)[0]?.entity_instance_id || ''
  }, [strategy])
  const refreshList = async (preferId?: string) => {
    const next = await fetchDispatchStrategies()
    setStrategies(next)
    const candidate = preferId || selectedId || next[0]?.id || ''
    if (candidate) setSelectedId(candidate)
  }

  useEffect(() => {
    Promise.all([fetchDispatchStrategies(), fetchEntityInstances()])
      .then(([strategyRows, entityRows]) => {
        setStrategies(strategyRows)
        setEntities(entityRows.items)
        if (!selectedId && strategyRows[0]) setSelectedId(strategyRows[0].id)
      })
      .catch((reason) => setError(describeDispatchStrategyError(reason)))
  }, [reloadNonce])

  useEffect(() => {
    if (!selectedId) {
      setStrategy(null)
      return
    }
    const request = loadGate.current.begin()
    setBusy('load')
    eventGeneration.current += 1
    setEventsBusy(false)
    setEventsError('')
    setControlEvidence(null)
    Promise.all([
      fetchDispatchStrategy(selectedId),
      fetchDispatchStrategyEvents(selectedId, { limit: eventPageSize }),
      fetchEntityInstances(),
    ])
      .then(([next, eventPage, entityRows]) => {
        if (!request.isCurrent()) return
        setStrategy(next)
        setEvents(eventPage.items)
        setNextEventCursor(eventPage.next_cursor)
        setEventCursor(null)
        setEventHistory([])
        setEntities(entityRows.items)
        setName(next.name)
        const source = next.draft || next.published_revision || next.active_revision
        const nextGraph = (source?.jdm_content || buildGenericDecisionTableJdm()) as DecisionGraphType
        setGraph(nextGraph)
        setTriggerKind(source?.trigger_kind || 'DATA_CHANGE')
        setShowGraph(false)
        setEditorPending(false)
        editorPendingRef.current = false
        setDraftBindings(source?.bindings ? structuredClone(source.bindings) : [])
        setBindingsEdited(false)
        setSimulation(null)
        setDraftReceipt(null)
        setNotice('')
        setRequiresReload(false)
        setError('')
      })
      .catch((reason) => { if (request.isCurrent()) setError(describeDispatchStrategyError(reason)) })
      .finally(() => { if (request.isCurrent()) setBusy('') })
    return request.cancel
  }, [selectedId, reloadNonce])

  useEffect(() => {
    const ids = [...new Set([...draftBindings.map((binding) => binding.entity_instance_id), selectedSummaryOutputId].filter(Boolean))]
    if (!ids.length) return
    Promise.allSettled(ids.map(async (id) => [id, await fetchEntityInstanceRealtime(id)] as const))
      .then((results) => setObservations((current) => {
        const next = { ...current }
        results.forEach((result, index) => {
          const id = ids[index]
          next[id] = result.status === 'fulfilled' ? result.value[1] : null
        })
        return next
      }))
  }, [draftBindings, selectedSummaryOutputId])

  const run = async (label: string, operation: () => Promise<void>) => {
    setBusy(label)
    setError('')
    setNotice('')
    try { await operation() }
    catch (reason) {
      const failure = dispatchStrategyFailureState(reason)
      setError(failure.message)
      setDraftReceipt(null)
      if (!failure.keepSimulation) setSimulation(null)
      setRequiresReload((current) => retainDispatchReloadLock(current, reason))
    }
    finally { setBusy('') }
  }

  const markEdited = () => {
    editGeneration.current += 1
    setSimulation(null)
    setDraftReceipt(null)
    setNotice('')
  }

  const handleEditorPending = (pending: boolean) => {
    editorPendingRef.current = pending
    setEditorPending(pending)
    if (pending) markEdited()
  }

  const createGeneric = () => run('create', async () => {
    const created = await createDispatchStrategy({ name: '通用调度策略' })
    if (!created.draft) throw new Error('服务端没有返回可编辑草稿。')
    const saved = await saveDispatchStrategyDraft(created.id, {
      expected_digest: created.draft.content_digest,
      name: '通用调度策略',
      description: created.description,
      trigger_kind: 'DATA_CHANGE',
      site_timezone: created.draft.site_timezone,
      base_configuration_revision: created.draft.base_configuration_revision,
      jdm_content: buildGenericDecisionTableJdm(),
      bindings: [],
    })
    await refreshList(saved.id)
    setSelectedId(saved.id)
    setNotice('已建立通用策略草稿，请绑定 L2 输入和可控输出。')
  })

  const saveDraft = async (): Promise<DispatchStrategy> => {
    if (editorPendingRef.current) throw new Error('原生编辑尚未确认，不能提交旧规则图。')
    if (!strategy || !currentRevision) throw new Error('请先选择策略。')
    const bindings = bindingsForDraft(draftBindings)
    if (nativeTable || bindingsEdited || bindings.length > 0) {
      if (!bindings.some((item) => item.direction === 'INPUT')) throw new Error('请至少绑定一个 L2 输入实体。')
      if (!bindings.some((item) => item.direction === 'OUTPUT')) throw new Error('请至少绑定一个可控 L2 输出实体。')
      const aliases = validateBindingAliases(bindings)
      if (!aliases.valid) throw new Error(aliases.message)
      for (const binding of bindings) {
        const entity = entities.find((item) => item.id === binding.entity_instance_id)
        const valid = binding.direction === 'INPUT' ? isNativeDecisionInputEntity(entity) : isNativeDecisionOutputEntity(entity)
        if (!valid) throw new Error(binding.direction === 'INPUT' ? `输入别名 ${binding.binding_key} 的实体不再可读或类型不受支持。` : `输出别名 ${binding.binding_key} 的实体没有明确的控制资格。`)
      }
    }
    const requestGeneration = editGeneration.current
    const saved = await saveDispatchStrategyDraft(strategy.id, {
      expected_digest: currentRevision.content_digest,
      name: name.trim(),
      description: strategy.description,
      trigger_kind: triggerKind,
      site_timezone: currentRevision.site_timezone,
      base_configuration_revision: currentRevision.base_configuration_revision,
      jdm_content: graph,
      bindings,
    })
    setStrategy(saved)
    await refreshList(saved.id)
    if (requestGeneration !== editGeneration.current) {
      throw new Error('保存期间草稿已继续编辑，服务器已保存请求发出时的版本；当前修改尚未保存，请再次保存。')
    }
    setGraph((saved.draft?.jdm_content || graph) as DecisionGraphType)
    setDraftBindings(saved.draft?.bindings ? structuredClone(saved.draft.bindings) : bindings)
    setBindingsEdited(false)
    if (saved.draft) setDraftReceipt({ id: saved.draft.id, digest: saved.draft.content_digest, revision: saved.draft.base_configuration_revision })
    return saved
  }

  const save = () => run('save', async () => {
    await saveDraft()
    setNotice('草稿已保存。')
  })

  const simulate = () => run('simulate', async () => {
    if (editorPendingRef.current) throw new Error('原生编辑尚未确认，不能试算旧规则图。')
    const requestGeneration = editGeneration.current
    setSimulation(null)
    if (!strategy || !currentRevision) throw new Error('请先选择策略。')
    const revision = dirty ? (await saveDraft()).draft : currentRevision
    if (!revision) throw new Error('没有可试算的策略版本。')
    const result = await simulateDispatchStrategy(strategy.id, {
      revision_id: revision.id,
      expected_digest: revision.content_digest,
    })
    if (requestGeneration !== editGeneration.current) {
      throw new Error('试算期间草稿已继续编辑，已丢弃旧试算结果；请保存当前草稿后重新试算。')
    }
    setSimulation(result)
    if (result.status === 'EVALUATED') setNotice('试算完成，没有向设备下发控制。')
    else setError('试算未通过，未执行计算，也未下发控制。')
  })

  const publish = () => run('publish', async () => {
    const saved = await saveDraft()
    if (!saved.draft) throw new Error('没有可发布的草稿。')
    await publishDispatchStrategy(saved.id, {
      expected_digest: saved.draft.content_digest,
      configuration_revision: saved.draft.base_configuration_revision,
    })
    const next = await fetchDispatchStrategy(saved.id)
    setStrategy(next)
    await refreshList(saved.id)
    setDraftReceipt(null)
    setNotice('已发布为不可变版本；确认后可启用。')
  })

  const enable = () => run('enable', async () => {
    if (editorPendingRef.current || !strategy?.published_revision || dirty || strategy.draft) throw new Error('请先保存并发布当前草稿。')
    const next = await enableDispatchStrategy(strategy.id, strategy.published_revision.id)
    setStrategy(next)
    await refreshList(next.id)
    setNotice(next.active_revision?.trigger_kind === 'DATA_CHANGE'
      ? '策略已启用，将在绑定的 L2 数据变化后运行。'
      : '策略已启用，将从下一个整分钟开始运行。')
  })

  const disable = () => run('disable', async () => {
    if (!strategy) return
    const next = await disableDispatchStrategy(strategy.id)
    setStrategy(next)
    await refreshList(next.id)
    setNotice('策略已停用，不再产生新的控制意图。')
  })

  const clearFailure = () => run('clear', async () => {
    if (!strategy) return
    const next = await clearDispatchStrategyFailure(strategy.id)
    setStrategy(next)
    setNotice('故障锁已清除，策略仍保持停用；确认安全后须显式启用。')
  })

  const patchGenericBinding = (index: number, patch: { alias?: string; entityId?: string }) => {
    setDraftBindings((current) => {
      const binding = current[index]
      if (!binding) return current
      if (patch.entityId !== undefined) {
        const entity = entities.find((item) => item.id === patch.entityId)
        if (!entity) return current
        return updateStrategyBinding(current, index, entity, patch.alias ?? binding.binding_key)
      }
      return current.map((item, itemIndex) => itemIndex === index ? { ...item, binding_key: patch.alias ?? item.binding_key } : item)
    })
    setBindingsEdited(true)
    markEdited()
  }

  const addGenericBinding = (direction: 'INPUT' | 'OUTPUT') => {
    const candidates = direction === 'INPUT' ? inputEntities : outputEntities
    const used = new Set(draftBindings.filter((item) => item.direction === direction).map((item) => item.entity_instance_id))
    const entity = candidates.find((item) => !used.has(item.id))
    if (!entity) {
      setError(direction === 'INPUT' ? '没有更多可读且类型受支持的 L2 输入实体。' : '没有更多明确具备控制资格的 L2 输出实体。')
      return
    }
    const ordinal = draftBindings.filter((item) => item.direction === direction).length
    const prefix = direction === 'INPUT' ? 'input' : 'output'
    const binding = makeStrategyBinding(entity, direction, `${prefix}_${ordinal + 1}`, ordinal)
    setDraftBindings((current) => [...current, binding])
    setBindingsEdited(true)
    markEdited()
    setError('')
  }

  const removeGenericBinding = (index: number) => {
    setDraftBindings((current) => {
      const direction = current[index]?.direction
      return current.filter((_item, itemIndex) => itemIndex !== index).map((item) => item.direction === direction
        ? { ...item, ordinal: current.filter((_candidate, candidateIndex) => candidateIndex !== index).filter((candidate) => candidate.direction === direction).findIndex((candidate) => candidate === item) }
        : item)
    })
    setBindingsEdited(true)
    markEdited()
  }

  const loadEventPage = async (cursor: string | null = null, history: (string | null)[] = [], size = eventPageSize) => {
    if (!selectedId) return
    const generation = ++eventGeneration.current
    setEventsBusy(true)
    setEventsError('')
    try {
      const page = await fetchDispatchStrategyEvents(selectedId, { limit: size, cursor })
      if (generation !== eventGeneration.current) return
      setEvents(page.items)
      setNextEventCursor(page.next_cursor)
      setEventCursor(cursor)
      setEventHistory(history)
      setEventPageSize(size)
    } catch (reason) {
      if (generation === eventGeneration.current) setEventsError(describeDispatchStrategyError(reason))
    } finally {
      if (generation === eventGeneration.current) setEventsBusy(false)
    }
  }

  const readControlEvidence = async (id: string) => {
    setControlEvidence({ id })
    try {
      const command = await fetchControlCommand(id)
      setControlEvidence((current) => current?.id === id ? { id, command } : current)
    } catch (reason) {
      setControlEvidence((current) => current?.id === id ? { id, error: describeDispatchStrategyError(reason) } : current)
    }
  }

  const renderBindings = (direction: 'INPUT' | 'OUTPUT') => {
    const label = direction === 'INPUT' ? '输入' : '输出'
    const candidates = direction === 'INPUT' ? inputEntities : outputEntities
    const rowsForDirection = draftBindings.map((binding, index) => ({ binding, index })).filter((item) => item.binding.direction === direction)
    return <div className="neu-inset p-3" data-testid={direction === 'INPUT' ? 'generic-l2-bindings' : undefined}>
      <div className="flex items-center justify-between gap-2"><div><h4 className="text-xs font-bold text-gray-700">{label}绑定</h4><p className="mt-1 text-[10px] text-gray-500">{direction === 'INPUT' ? '已确认、可读的布尔/数值/字符串 L2' : '已确认、明确可控且可写的 L2'}</p></div><button type="button" onClick={() => addGenericBinding(direction)} className="neu-btn px-3 py-1.5 text-xs">添加{label}</button></div>
      <div className="mt-3 space-y-3">{rowsForDirection.map(({ binding, index }, visibleIndex) => {
        const currentEntity = entities.find((item) => item.id === binding.entity_instance_id)
        return <div key={`${direction}:${index}`} className="rounded-lg border border-white/70 bg-white/35 p-3">
          <label className="block text-[11px] font-semibold text-gray-600">{label} {visibleIndex + 1} 别名<input aria-label={`${label} ${visibleIndex + 1} 别名`} value={binding.binding_key} onChange={(event) => patchGenericBinding(index, { alias: event.target.value })} className="neu-input mt-1 w-full px-2 py-1.5 font-mono" /></label>
          <label className="mt-2 block text-[11px] font-semibold text-gray-600">{label} {visibleIndex + 1} 实体<select aria-label={`${label} ${visibleIndex + 1} 实体`} value={binding.entity_instance_id} onChange={(event) => patchGenericBinding(index, { entityId: event.target.value })} className="neu-input mt-1 w-full px-2 py-1.5"><option value="">请选择</option>{currentEntity && !candidates.some((item) => item.id === currentEntity.id) && <option value={currentEntity.id} disabled>当前绑定不再符合资格：{currentEntity.display_name}</option>}{candidates.map((item) => <option key={item.id} value={item.id}>{item.node_display_name} / {item.display_name} · {item.data_type} {item.unit || ''}</option>)}</select></label>
          <div className="mt-2 flex items-center justify-between gap-2 text-[10px] text-gray-500"><span>{binding.expected_data_type} {binding.unit || '无单位'} · 新鲜度 {binding.freshness_seconds}s · 质量：{qualityText(observations[binding.entity_instance_id])}</span><button type="button" onClick={() => removeGenericBinding(index)} className="text-red-600">移除</button></div>
        </div>
      })}{!rowsForDirection.length && <p className="py-3 text-center text-xs text-amber-700">尚未添加{label}绑定。</p>}</div>
    </div>
  }

  return (
    <div className="tablet-applications tablet-dispatch-layout flex min-h-[650px] gap-4" data-tablet-applications="dispatch" data-testid="dispatch-strategy-page">
      <aside className="neu-card w-72 shrink-0 p-4">
        <div className="mb-4 flex items-center justify-between">
          <div><h2 className="text-sm font-bold text-gray-800">调度策略</h2><p className="mt-1 text-[11px] text-gray-500">基于 L2 决策，经统一控制闭环执行</p></div>
          <div className="flex flex-col items-end gap-2"><button type="button" onClick={createGeneric} disabled={!!busy} className="neu-btn zizu-primary px-3 py-1.5 text-xs font-semibold">新建通用策略</button></div>
        </div>
        <div className="space-y-2" aria-label="策略列表">
          {strategies.map((item) => {
            const itemStatus = projectStrategyStatus(item)
            const currentOutput = item.id === selectedId && selectedSummaryOutputId
              ? observations[selectedSummaryOutputId]
              : null
            return <button key={item.id} type="button" disabled={!!busy || editorPending} onClick={() => setSelectedId(item.id)} className={`w-full rounded-xl border p-3 text-left ${selectedId === item.id ? 'zizu-tab-active' : 'border-white/60 bg-white/30'}`}>
              <div className="truncate text-xs font-semibold text-gray-800">{item.name}</div>
              <div className="mt-2 flex flex-wrap gap-1 text-[10px]"><span>{itemStatus.enableLabel}</span><span>·</span><span>{itemStatus.lifecycleLabel}</span><span>·</span><span>{itemStatus.healthLabel}</span></div>
              <div className="mt-1 text-[10px] text-gray-400">目标 {valueText(item.last_desired)} / {currentOutput ? '当前 L2' : '决策时值'} {valueText(currentOutput?.value ?? item.last_actual)}</div>
            </button>
          })}
          {!strategies.length && !error && <p className="py-8 text-center text-xs text-gray-400">尚无策略，请新建通用策略。</p>}
        </div>
      </aside>

      <main className="min-w-0 flex-1 space-y-4">
          {(error || notice) && <div role={error ? 'alert' : 'status'} className={`flex min-h-11 flex-wrap items-center justify-between gap-3 rounded-lg border px-4 py-3 text-xs ${error ? 'border-red-200 bg-red-50 text-red-700' : 'border-green-200 bg-green-50 text-green-700'}`}><span>{error || notice}</span>{(requiresReload || (!strategy && error)) && <button type="button" className="neu-btn min-h-11 px-4 text-xs font-semibold text-[#981320]" onClick={() => setReloadNonce((value) => value + 1)}>重新加载策略</button>}</div>}
        {!strategy ? <div className="neu-card flex min-h-[500px] items-center justify-center text-sm text-gray-400">{error ? '策略读取失败，请查看上方原因。' : busy ? '正在读取调度策略…' : '请选择或新建调度策略'}</div> : <>
          <section className="neu-card p-4" aria-label="策略状态">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-[260px] flex-1"><label className="text-xs font-semibold text-gray-600">策略名称<input aria-label="策略名称" value={name} onChange={(event) => { setName(event.target.value); markEdited() }} className="neu-input mt-1 w-full px-3 py-2 text-sm" /></label><p className="mt-2 text-[11px] text-gray-500">{triggerKind === 'DATA_CHANGE' ? 'L2 数据变化触发' : '固定整分钟节拍'} · {currentRevision?.site_timezone} · 所有控制先形成意图，再由统一控制回读确认</p></div>
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <label className="text-gray-600">触发方式<select aria-label="触发方式" className="neu-input ml-2 px-2" value={triggerKind} onChange={(event) => { setTriggerKind(event.target.value as 'DATA_CHANGE' | 'FIXED_TICK'); markEdited() }}><option value="DATA_CHANGE">L2 数据变化</option><option value="FIXED_TICK">固定整分钟</option></select></label>
                {dirty && <span className="rounded bg-amber-100 px-2 py-1 text-amber-800">未保存修改 · 旧试算及收据已失效</span>}
                <span className="rounded bg-indigo-50 px-2 py-1 text-indigo-700">{status?.lifecycleLabel} {status?.publishedRevision ? `v${status.publishedRevision}` : ''}</span>
                <span className={`rounded px-2 py-1 ${strategy.enabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{status?.enableLabel}</span>
                <span className={`rounded px-2 py-1 ${HEALTH_STYLES[strategy.runtime_health] || 'bg-gray-100'}`}>{status?.healthLabel}</span>
              </div>
            </div>
          </section>



          <section className="neu-card p-4" aria-labelledby="binding-heading">
            <h3 id="binding-heading" className="mb-3 text-sm font-bold text-gray-800">1. 选择 L2 输入</h3>
            <p className="mb-3 text-xs text-gray-500">选择多个强类型实体，以别名引用；跨设备只读取已提交 L2，不读取品牌点位。</p>
            {renderBindings('INPUT')}
          </section>

          <section className="neu-card p-4" aria-labelledby="schedule-heading">
            <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
              <div><h3 id="schedule-heading" className="text-sm font-bold text-gray-800">2. 编辑原生 JDM 决策表</h3><p className="mt-1 text-xs text-gray-500">通用原生决策表直接编辑现有唯一表节点，不限制为 SOC 或固定时段。</p></div>
              <button type="button" disabled={editorPending} onClick={() => setShowGraph((value) => !value)} className="neu-btn px-3 text-xs text-[#981320]">{showGraph ? '收起完整规则图' : '打开完整规则图'}</button>
            </div>
            {nativeTable ? <>
              <div className="mb-3 flex flex-wrap items-center gap-3"><button type="button" disabled={editorPending} className="neu-btn px-3 text-xs" onClick={() => { setGraph(addNativeExampleColumns(graph, nativeTable.nodeId) as DecisionGraphType); markEdited() }}>添加可选示例列</button><span className="text-xs text-gray-500">时段 / SOC 只是可删除的原生条件列，不新增规则或输出目标。</span></div>
              {!showGraph && <Suspense fallback={<p aria-live="polite">正在加载原生决策表…</p>}><NativeDecisionTableEditor key={editorSession} onPendingChange={handleEditorPending} content={nativeTable.content} onChange={(content) => { setGraph((current) => replaceDecisionTableContent(current, nativeTable.nodeId, content) as DecisionGraphType); markEdited() }} /></Suspense>}
            </> : <p className="rounded-lg bg-amber-50 p-3 text-xs text-amber-800">当前规则不是可无损往返的唯一决策表，请使用完整规则图编辑。保存、试算和发布均使用同一份完整 JDM，不会重建为内置表。</p>}
            {showGraph && <Suspense fallback={<p aria-live="polite">正在加载完整规则图…</p>}><NativeDecisionGraphEditor key={editorSession} graph={editorGraph} onPendingChange={handleEditorPending} onChange={(next) => { if (isJdmGraphUnchanged(next, editorGraph)) return; setGraph((current) => mergeNativeDecisionGraph(current, next) as DecisionGraphType); markEdited() }} /></Suspense>}
          </section>

          <section className="neu-card p-4" aria-labelledby="output-heading">
            <h3 id="output-heading" className="mb-3 text-sm font-bold text-gray-800">3. 绑定可控 L2 输出</h3>
            <p className="mb-3 text-xs text-gray-500">action_id 对应输出别名；多个目标按意图顺序交给唯一控制链。接口受理不是设备执行成功。</p>
            {renderBindings('OUTPUT')}
            {!outputEntities.length && <p className="mt-3 text-xs text-amber-700">没有明确具备控制资格的输出候选。请先配置正式 L2 控制合同；保存、发布和运行仍由后端复核。</p>}
          </section>

          <section className="neu-card p-4" aria-labelledby="verification-heading">
            <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 id="verification-heading" className="text-sm font-bold text-gray-800">草稿、试算与运行</h3><p className="mt-1 text-xs text-gray-500">试算不下发；发布冻结版本；启用后按已保存的触发方式产生控制意图。</p></div><div className="flex flex-wrap gap-2"><button type="button" onClick={save} disabled={!!busy || requiresReload || editorPending} className="neu-btn px-3 py-1.5 text-xs disabled:opacity-40">保存草稿</button><button type="button" onClick={simulate} disabled={!!busy || requiresReload || editorPending} className="neu-btn px-3 py-1.5 text-xs text-indigo-700 disabled:opacity-40">试算</button><button type="button" onClick={publish} disabled={!!busy || requiresReload || editorPending} className="neu-btn zizu-primary px-3 py-1.5 text-xs disabled:opacity-40">发布</button>{strategy.enabled ? <button type="button" onClick={disable} disabled={!!busy} className="neu-btn px-3 py-1.5 text-xs text-red-600">停用</button> : <button type="button" onClick={enable} disabled={!!busy || requiresReload || dirty || !!strategy.draft || !strategy.published_revision || strategy.runtime_health === 'FAILED'} className="zizu-primary rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-40">启用</button>}{strategy.runtime_health === 'FAILED' && <button type="button" onClick={clearFailure} disabled={!!busy} className="neu-btn px-3 py-1.5 text-xs text-red-600">清除故障锁</button>}</div></div>
            {editorPending && <div aria-live="polite" className="mt-3 text-xs text-amber-700">正在同步原生编辑内容，收到确认后可保存、试算和发布。若仅打开菜单或取消编辑而没有内容回调，可放弃尚未确认的编辑，返回最后已同步图。<button type="button" className="ml-2 underline" onClick={() => { if (!window.confirm('放弃尚未收到原生确认的编辑？将返回最后已同步规则图；已同步但未保存的修改仍会保留。')) return; setEditorSession((value) => value + 1); handleEditorPending(false) }}>放弃未确认编辑</button></div>}
            {draftReceipt && <div data-testid="strategy-draft-receipt" className="mt-3 break-all rounded-lg border border-green-200 bg-green-50 p-3 text-xs text-green-800">草稿收据：{draftReceipt.id} · 配置修订 {draftReceipt.revision} · 摘要 {draftReceipt.digest}</div>}
            {simulation && <div className="mt-4 space-y-3" data-testid="strategy-simulation">
              {simulation.status !== 'EVALUATED' && <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
                {describeDispatchStrategyError({ code: simulation.reason_code, message: '试算未通过，请检查实体绑定与数据状态。' })}
                {simulation.reason_code && <span className="ml-2 font-mono text-[10px]">{simulation.reason_code}</span>}
              </div>}
              <div className="grid gap-3 md:grid-cols-4">
                <div className="neu-inset p-3"><div className="text-[10px] text-gray-500">试算状态</div><div className="mt-1 text-xs font-semibold">{simulation.status === 'EVALUATED' ? '已完成试算（未下发）' : '未执行计算'}</div></div>
                <div className="neu-inset p-3"><div className="text-[10px] text-gray-500">快照证据</div><div className="mt-1 text-xs">帧 {simulation.frame_sequence ?? '—'} · 配置 {simulation.configuration_revision ?? '—'} · {Object.keys(simulation.snapshot).length} 个实体</div></div>
                <div className="neu-inset p-3"><div className="text-[10px] text-gray-500">命中行</div><div className="mt-1 text-xs font-semibold">{simulation.status === 'EVALUATED' ? simulation.matched_rules.join('、') || '未命中' : '未执行计算'}</div></div>
                <div className="neu-inset p-3"><div className="text-[10px] text-gray-500">拟执行意图</div><div className="mt-1 text-xs font-semibold">{simulation.status === 'EVALUATED' ? simulation.proposed_intents.map((item) => `${item.action_id}=${valueText(item.value)}`).join('、') || '无需控制' : '未生成控制意图'}</div></div>
              </div>
              <div className="overflow-x-auto"><table className="w-full text-xs" aria-label="本次试算数据依据">
                <caption className="py-2 text-left text-gray-500">本次试算数据依据（固定快照，采集质量正常不代表数据未超时）</caption>
                <thead><tr className="border-b text-left text-gray-500"><th className="p-2">实体 / 绑定</th><th className="p-2">采集值</th><th className="p-2">单位</th><th className="p-2">采集质量</th><th className="p-2">数据时间</th></tr></thead>
                <tbody>{Object.entries(simulation.snapshot).map(([key, sample]) => <tr key={key} className="border-b border-white/60">
                  <td className="p-2">{entities.find((item) => item.id === sample.entity_instance_id)?.display_name || key}<span className="ml-1 text-gray-400">{key}</span></td>
                  <td className="p-2">{valueText(sample.value)}</td><td className="p-2">{sample.unit || '—'}</td><td className="p-2">{sample.quality || '未知'}</td><td className="p-2">{sample.observed_at ? new Date(sample.observed_at).toLocaleString() : '缺少时间'}</td>
                </tr>)}{!Object.keys(simulation.snapshot).length && <tr><td colSpan={5} className="p-3 text-gray-500">没有可用的已提交数据，请检查实体数据链路。</td></tr>}</tbody>
              </table></div>
            </div>}
          </section>

          <section className="neu-card p-4" aria-labelledby="events-heading"><div className="mb-3 flex items-center justify-between"><div><h3 id="events-heading" className="text-sm font-bold text-gray-800">4. 关键事件与控制回读</h3><p className="mt-1 text-xs text-gray-500">只读运行证据；accepted / dispatched 仅表示受理或等待回读，readback_confirmed 才是已确认到位。</p></div><button type="button" disabled={eventsBusy} onClick={() => loadEventPage()} className="neu-btn px-3 py-1.5 text-xs">刷新</button></div><div className="overflow-x-auto"><table className="w-full min-w-[760px] text-xs"><thead><tr className="border-b text-left text-gray-500"><th className="p-2">时间</th><th className="p-2">事件</th><th className="p-2">原因/命中</th><th className="p-2">控制命令</th><th className="p-2">回读状态</th></tr></thead><tbody>{events.map((event) => <tr key={event.id} className="border-b border-white/60"><td className="p-2">{new Date(event.occurred_at).toLocaleString()}</td><td className="p-2 font-medium">{event.event_kind}</td><td className="p-2">{event.reason_code || valueText(event.decision?.matched_rule)}</td><td className="p-2 font-mono text-[10px]">{event.control_command_id ? <button type="button" className="text-[#981320] underline" aria-label={`查看控制回读 ${event.control_command_id}`} onClick={() => readControlEvidence(event.control_command_id!)}>{event.control_command_id}</button> : '—'}</td><td className="p-2">{event.control_status === 'readback_confirmed' ? '回读确认到位' : ['accepted', 'validated', 'dispatched'].includes(event.control_status || '') ? '等待回读' : event.control_status || '—'}<details className="mt-1"><summary className="cursor-pointer text-[#981320]">查看证据</summary><pre className="max-w-sm whitespace-pre-wrap break-all text-[10px]">{JSON.stringify({ frame_sequence: event.frame_sequence, configuration_revision: event.configuration_revision, snapshot: event.snapshot_evidence, decision: event.decision, intents: event.intent_summary, command_id: event.control_command_id, control_status: event.control_status }, null, 2)}</pre></details></td></tr>)}{!events.length && <tr><td colSpan={5} className="p-6 text-center text-gray-400">暂无关键事件</td></tr>}</tbody></table></div>
            {eventsError && <p role="alert" className="mt-3 text-xs text-red-700">事件读取失败：{eventsError}。上表保留上次证据，不代表最新状态。</p>}
            {eventsBusy && <p aria-live="polite" className="mt-3 text-xs text-gray-500">正在读取运行事件…</p>}
            <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-xs"><label>每页条数<select aria-label="事件每页条数" value={eventPageSize} disabled={eventsBusy} onChange={(event) => loadEventPage(null, [], Number(event.target.value))} className="neu-input ml-2 px-2"><option value={10}>10</option><option value={20}>20</option></select></label><span>第 {eventHistory.length + 1} 页 · 本页 {events.length} 条</span><div className="flex gap-2"><button type="button" className="neu-btn px-3 disabled:opacity-40" disabled={eventsBusy || !eventHistory.length} onClick={() => loadEventPage(eventHistory[eventHistory.length - 1], eventHistory.slice(0, -1))}>上一页</button><button type="button" className="neu-btn px-3 disabled:opacity-40" disabled={eventsBusy || !nextEventCursor} onClick={() => loadEventPage(nextEventCursor, [...eventHistory, eventCursor])}>下一页</button></div></div>
          </section>
          {controlEvidence && <section className="neu-card p-4" aria-label="控制回读证据" data-testid="strategy-control-evidence"><div className="flex items-center justify-between gap-3"><h3 className="text-sm font-bold">控制回读证据</h3><button type="button" className="neu-btn px-3 text-xs" onClick={() => setControlEvidence(null)}>关闭证据</button></div><p className="mt-2 text-xs">{controlEvidence.command?.status === 'readback_confirmed' ? '回读确认到位（后端新 committed L2 证据）' : '未确认执行成功；仅按后端控制命令状态判断，不将接口受理当设备动作。'}</p>{controlEvidence.error ? <p role="alert" className="mt-2 text-xs text-red-700">{controlEvidence.error}</p> : controlEvidence.command ? <pre className="mt-3 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(controlEvidence.command, null, 2)}</pre> : <p aria-live="polite" className="mt-3 text-xs">正在读取控制命令…</p>}</section>}
        </>}
      </main>
    </div>
  )
}
