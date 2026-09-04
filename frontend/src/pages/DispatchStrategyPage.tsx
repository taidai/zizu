import { useEffect, useMemo, useState } from 'react'
import { DecisionGraph, JdmConfigProvider, ensureWasmLoaded } from '@gorules/jdm-editor'
import { DndProvider } from 'react-dnd'
import { HTML5Backend } from 'react-dnd-html5-backend'
import '../monaco'
import '@gorules/jdm-editor/dist/style.css'
import {
  clearDispatchStrategyFailure,
  createDispatchStrategy,
  disableDispatchStrategy,
  enableDispatchStrategy,
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
  type DispatchStrategyRevision,
  type DispatchStrategySimulation,
  type EntityInstance,
  type EntityInstanceObservation,
} from '../api/client'
import {
  buildTwoChargeTwoDischargeJdm,
  describeDispatchStrategyError,
  makeStrategyBinding,
  projectStrategyStatus,
  validateDispatchWindows,
  type DispatchWindow,
} from '../components/dispatch-strategy/dispatchStrategyModel.mjs'

ensureWasmLoaded().catch(() => {})

const DEFAULT_ROWS: DispatchWindow[] = [
  { key: 'charge-1', start: '00:00', end: '06:00', action: 'CHARGE', target: -60, socMin: 10, socMax: 90 },
  { key: 'discharge-1', start: '10:00', end: '12:00', action: 'DISCHARGE', target: 80, socMin: 10, socMax: 90 },
  { key: 'charge-2', start: '12:00', end: '14:00', action: 'CHARGE', target: -60, socMin: 10, socMax: 90 },
  { key: 'discharge-2', start: '18:00', end: '22:00', action: 'DISCHARGE', target: 80, socMin: 10, socMax: 90 },
]

type DecisionGraphType = { nodes: any[]; edges: any[]; [key: string]: any }

const ACTION_LABELS = { CHARGE: '充电', DISCHARGE: '放电', HOLD: '保持' }
const HEALTH_STYLES: Record<string, string> = {
  READY: 'bg-green-100 text-green-700',
  IDLE: 'bg-gray-100 text-gray-600',
  BLOCKED: 'bg-amber-100 text-amber-700',
  FAILED: 'bg-red-100 text-red-700',
}

function numberFrom(expression: unknown, operator: '>=' | '<='): number | null {
  const match = new RegExp(`${operator}\\s*(-?\\d+(?:\\.\\d+)?)`).exec(String(expression))
  return match ? Number(match[1]) : null
}

function readEasyTable(revision: DispatchStrategyRevision | null): { rows: DispatchWindow[]; safeTarget: number } {
  const table = revision?.jdm_content?.nodes?.find((node: any) => node.type === 'decisionTableNode')?.content
  if (!Array.isArray(table?.rules)) return { rows: DEFAULT_ROWS, safeTarget: 0 }
  const rows: DispatchWindow[] = []
  let safeTarget = 0
  for (const rule of table.rules) {
    if (rule?._id === 'other-time') {
      safeTarget = Number(rule.target ?? 0)
      continue
    }
    const time = String(rule?.site_local_minute || '').match(/>=\s*(\d+)\s*&&\s*site_local_minute\s*<\s*(\d+)/)
    if (!time) continue
    const toTime = (minutes: number) => minutes === 1440
      ? '24:00'
      : `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`
    rows.push({
      key: String(rule._id),
      start: toTime(Number(time[1])),
      end: toTime(Number(time[2])),
      action: ['CHARGE', 'DISCHARGE', 'HOLD'].includes(rule._description) ? rule._description : 'HOLD',
      target: Number(rule.target ?? 0),
      socMin: numberFrom(rule.soc, '>=') ?? 0,
      socMax: numberFrom(rule.soc, '<=') ?? 100,
    })
  }
  return { rows: rows.length ? rows : DEFAULT_ROWS, safeTarget }
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
  const [name, setName] = useState('')
  const [rows, setRows] = useState<DispatchWindow[]>(DEFAULT_ROWS)
  const [safeTarget, setSafeTarget] = useState<number | string>(0)
  const [socId, setSocId] = useState('')
  const [outputId, setOutputId] = useState('')
  const [graph, setGraph] = useState<DecisionGraphType>(() => buildTwoChargeTwoDischargeJdm(DEFAULT_ROWS, 0) as DecisionGraphType)
  const [showGraph, setShowGraph] = useState(false)
  const [graphCustomized, setGraphCustomized] = useState(false)
  const [simulation, setSimulation] = useState<DispatchStrategySimulation | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const currentRevision = strategy?.draft || strategy?.published_revision || strategy?.active_revision || null
  const validation = useMemo(() => validateDispatchWindows(rows, safeTarget), [rows, safeTarget])
  const status = strategy ? projectStrategyStatus(strategy) : null

  const inputEntities = useMemo(
    () => entities.filter((item) => item.confirmed && ['R', 'RW'].includes(item.direction) && ['FLOAT', 'INT'].includes(item.data_type.toUpperCase())),
    [entities],
  )
  const outputEntities = useMemo(
    () => entities.filter((item) => item.confirmed && ['W', 'RW'].includes(item.direction) && ['FLOAT', 'INT'].includes(item.data_type.toUpperCase())),
    [entities],
  )

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
        if (strategyRows[0]) setSelectedId(strategyRows[0].id)
      })
      .catch((reason) => setError(describeDispatchStrategyError(reason)))
  }, [])

  useEffect(() => {
    if (!selectedId) {
      setStrategy(null)
      return
    }
    setBusy('load')
    Promise.all([
      fetchDispatchStrategy(selectedId),
      fetchDispatchStrategyEvents(selectedId, { limit: 30 }),
    ])
      .then(([next, eventPage]) => {
        setStrategy(next)
        setEvents(eventPage.items)
        setName(next.name)
        const source = next.draft || next.published_revision || next.active_revision
        const easy = readEasyTable(source)
        setRows(easy.rows)
        setSafeTarget(easy.safeTarget)
        setGraph((source?.jdm_content || buildTwoChargeTwoDischargeJdm(easy.rows, easy.safeTarget)) as DecisionGraphType)
        setGraphCustomized(false)
        setSocId(source?.bindings.find((item) => item.direction === 'INPUT' && item.binding_key === 'soc')?.entity_instance_id || '')
        setOutputId(source?.bindings.find((item) => item.direction === 'OUTPUT' && item.binding_key === 'power-target')?.entity_instance_id || '')
        setSimulation(null)
        setError('')
      })
      .catch((reason) => setError(describeDispatchStrategyError(reason)))
      .finally(() => setBusy(''))
  }, [selectedId])

  useEffect(() => {
    const ids = [...new Set([socId, outputId].filter(Boolean))]
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
  }, [socId, outputId])

  const run = async (label: string, operation: () => Promise<void>) => {
    setBusy(label)
    setError('')
    setNotice('')
    try { await operation() }
    catch (reason) { setError(describeDispatchStrategyError(reason)) }
    finally { setBusy('') }
  }

  const createNew = () => run('create', async () => {
    const created = await createDispatchStrategy({ name: '2充2放调度策略' })
    await refreshList(created.id)
    setSelectedId(created.id)
    setNotice('已建立策略草稿，请绑定 SOC 和功率控制实体。')
  })

  const saveDraft = async (): Promise<DispatchStrategy> => {
    if (!strategy || !currentRevision) throw new Error('请先选择策略。')
    if (!validation.valid) throw new Error(validation.message)
    const soc = entities.find((item) => item.id === socId)
    const output = entities.find((item) => item.id === outputId)
    if (!soc) throw new Error('请选择 SOC 全局实体。')
    if (!output) throw new Error('请选择功率控制全局实体。')
    const easyGraph = graphCustomized ? graph : buildTwoChargeTwoDischargeJdm(rows, safeTarget)
    const saved = await saveDispatchStrategyDraft(strategy.id, {
      expected_digest: currentRevision.content_digest,
      name: name.trim(),
      description: strategy.description,
      trigger_kind: 'FIXED_TICK',
      site_timezone: currentRevision.site_timezone || 'Asia/Shanghai',
      base_configuration_revision: currentRevision.base_configuration_revision,
      jdm_content: easyGraph,
      bindings: [
        makeStrategyBinding(soc, 'INPUT', 'soc', 0),
        makeStrategyBinding(output, 'OUTPUT', 'power-target', 0),
      ],
    })
    setStrategy(saved)
    setGraph((saved.draft?.jdm_content || easyGraph) as DecisionGraphType)
    await refreshList(saved.id)
    return saved
  }

  const save = () => run('save', async () => {
    await saveDraft()
    setNotice('草稿已保存。')
  })

  const simulate = () => run('simulate', async () => {
    const saved = await saveDraft()
    const result = await simulateDispatchStrategy(saved.id, { revision_id: saved.draft?.id })
    setSimulation(result)
    setNotice('试算完成，没有向设备下发控制。')
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
    setNotice('已发布为不可变版本；确认后可启用。')
  })

  const enable = () => run('enable', async () => {
    if (!strategy?.published_revision) throw new Error('请先发布策略。')
    const next = await enableDispatchStrategy(strategy.id, strategy.published_revision.id)
    setStrategy(next)
    await refreshList(next.id)
    setNotice('策略已启用，将从下一个整分钟开始运行。')
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
    setNotice('故障锁已清除。')
  })

  const patchRow = (index: number, patch: Partial<DispatchWindow>) => {
    setRows((current) => current.map((row, itemIndex) => itemIndex === index ? { ...row, ...patch } : row))
    setGraphCustomized(false)
    setSimulation(null)
  }

  return (
    <div className="flex min-h-[650px] gap-4" data-testid="dispatch-strategy-page">
      <aside className="neu-card w-72 shrink-0 p-4">
        <div className="mb-4 flex items-center justify-between">
          <div><h2 className="text-sm font-bold text-gray-800">调度策略</h2><p className="mt-1 text-[11px] text-gray-500">定时决策，经统一控制闭环执行</p></div>
          <button type="button" onClick={createNew} disabled={!!busy} className="neu-btn px-3 py-1.5 text-xs font-semibold text-[#287c12]">新建 2充2放</button>
        </div>
        <div className="space-y-2" aria-label="策略列表">
          {strategies.map((item) => {
            const itemStatus = projectStrategyStatus(item)
            return <button key={item.id} type="button" onClick={() => setSelectedId(item.id)} className={`w-full rounded-xl border p-3 text-left ${selectedId === item.id ? 'border-[#52c41a] bg-[#52c41a]/10' : 'border-white/60 bg-white/30'}`}>
              <div className="truncate text-xs font-semibold text-gray-800">{item.name}</div>
              <div className="mt-2 flex flex-wrap gap-1 text-[10px]"><span>{itemStatus.enableLabel}</span><span>·</span><span>{itemStatus.lifecycleLabel}</span><span>·</span><span>{itemStatus.healthLabel}</span></div>
              <div className="mt-1 text-[10px] text-gray-400">目标 {valueText(item.last_desired)} / 回读 {valueText(item.last_actual)}</div>
            </button>
          })}
          {!strategies.length && <p className="py-8 text-center text-xs text-gray-400">尚无策略，点击“新建 2充2放”。</p>}
        </div>
      </aside>

      <main className="min-w-0 flex-1 space-y-4">
        {!strategy ? <div className="neu-card flex min-h-[500px] items-center justify-center text-sm text-gray-400">请选择或新建调度策略</div> : <>
          <section className="neu-card p-4" aria-label="策略状态">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-[260px] flex-1"><label className="text-xs font-semibold text-gray-600">策略名称<input aria-label="策略名称" value={name} onChange={(event) => setName(event.target.value)} className="neu-input mt-1 w-full px-3 py-2 text-sm" /></label><p className="mt-2 text-[11px] text-gray-500">固定整分钟节拍 · Asia/Shanghai · 所有控制先形成意图，再由统一控制回读确认</p></div>
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="rounded bg-indigo-50 px-2 py-1 text-indigo-700">{status?.lifecycleLabel} {status?.publishedRevision ? `v${status.publishedRevision}` : ''}</span>
                <span className={`rounded px-2 py-1 ${strategy.enabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{status?.enableLabel}</span>
                <span className={`rounded px-2 py-1 ${HEALTH_STYLES[strategy.runtime_health] || 'bg-gray-100'}`}>{status?.healthLabel}</span>
              </div>
            </div>
          </section>

          {(error || notice) && <div role={error ? 'alert' : 'status'} className={`rounded-lg border px-4 py-3 text-xs ${error ? 'border-red-200 bg-red-50 text-red-700' : 'border-green-200 bg-green-50 text-green-700'}`}>{error || notice}</div>}

          <section className="neu-card p-4" aria-labelledby="binding-heading">
            <div className="mb-3"><h3 id="binding-heading" className="text-sm font-bold text-gray-800">1. 绑定 L2 全局实体</h3><p className="mt-1 text-xs text-gray-500">策略只认稳定实体，不直接使用品牌点位。这里只显示已确认、类型合适的实体。</p></div>
            <div className="grid gap-3 md:grid-cols-2">
              <label className="text-xs font-semibold text-gray-600">SOC 输入实体
                <select aria-label="SOC 输入实体" value={socId} onChange={(event) => setSocId(event.target.value)} className="neu-input mt-1 w-full px-3 py-2">
                  <option value="">请选择</option>{inputEntities.map((item) => <option key={item.id} value={item.id}>{item.node_display_name} / {item.display_name} · {item.data_type} {item.unit || ''}</option>)}
                </select>
                {socId && <span className="mt-2 block font-normal text-gray-500">质量：{qualityText(observations[socId])} · 新鲜度 {entities.find((item) => item.id === socId)?.freshness_seconds}s · 当前值 {valueText(observations[socId]?.value)}</span>}
              </label>
              <label className="text-xs font-semibold text-gray-600">功率控制实体
                <select aria-label="功率控制实体" value={outputId} onChange={(event) => setOutputId(event.target.value)} className="neu-input mt-1 w-full px-3 py-2">
                  <option value="">请选择</option>{outputEntities.map((item) => <option key={item.id} value={item.id}>{item.node_display_name} / {item.display_name} · {item.direction} · {item.data_type} {item.unit || ''}</option>)}
                </select>
                {outputId && <span className="mt-2 block font-normal text-gray-500">可控：是 · 质量：{qualityText(observations[outputId])} · 当前回读 {valueText(observations[outputId]?.value)}</span>}
              </label>
            </div>
          </section>

          <section className="neu-card p-4" aria-labelledby="schedule-heading">
            <div className="mb-3 flex items-start justify-between gap-3"><div><h3 id="schedule-heading" className="text-sm font-bold text-gray-800">2. 设置 2充2放</h3><p className="mt-1 text-xs text-gray-500">正功率表示放电，负功率表示充电；重叠时间会在保存前拦住。</p></div><button type="button" onClick={() => setShowGraph((value) => !value)} className="neu-btn px-3 py-1.5 text-xs text-indigo-700">{showGraph ? '收起完整规则图' : '打开完整规则图'}</button></div>
            <div className="overflow-x-auto"><table className="w-full min-w-[760px] text-xs"><thead><tr className="border-b text-left text-gray-500"><th className="p-2">时段</th><th className="p-2">开始</th><th className="p-2">结束</th><th className="p-2">动作</th><th className="p-2">功率目标</th><th className="p-2">SOC 下限</th><th className="p-2">SOC 上限</th></tr></thead><tbody>{rows.map((row, index) => <tr key={row.key} className={`border-b border-white/60 ${validation.overlapKeys.includes(row.key) ? 'bg-red-50' : ''}`}><td className="p-2 font-medium">{index + 1}</td><td className="p-1"><input aria-label={`时段 ${index + 1} 开始`} type="time" value={row.start} onChange={(event) => patchRow(index, { start: event.target.value })} className="neu-input w-full px-2 py-1.5" /></td><td className="p-1"><input aria-label={`时段 ${index + 1} 结束`} type="time" value={row.end === '24:00' ? '23:59' : row.end} onChange={(event) => patchRow(index, { end: event.target.value })} className="neu-input w-full px-2 py-1.5" /></td><td className="p-1"><select aria-label={`时段 ${index + 1} 动作`} value={row.action} onChange={(event) => patchRow(index, { action: event.target.value as DispatchWindow['action'] })} className="neu-input w-full px-2 py-1.5">{Object.entries(ACTION_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></td><td className="p-1"><input aria-label={`时段 ${index + 1} 功率目标`} type="number" step="0.1" value={row.target} onChange={(event) => patchRow(index, { target: event.target.value })} className="neu-input w-full px-2 py-1.5" /></td><td className="p-1"><input aria-label={`时段 ${index + 1} SOC 下限`} type="number" min="0" max="100" value={row.socMin} onChange={(event) => patchRow(index, { socMin: event.target.value })} className="neu-input w-full px-2 py-1.5" /></td><td className="p-1"><input aria-label={`时段 ${index + 1} SOC 上限`} type="number" min="0" max="100" value={row.socMax} onChange={(event) => patchRow(index, { socMax: event.target.value })} className="neu-input w-full px-2 py-1.5" /></td></tr>)}</tbody></table></div>
            <div className="mt-3 flex flex-wrap items-center gap-3"><label className="text-xs font-semibold text-gray-600">其他时段安全目标 <input aria-label="其他时段安全目标" type="number" step="0.1" value={safeTarget} onChange={(event) => { setSafeTarget(event.target.value); setGraphCustomized(false) }} className="neu-input ml-2 w-32 px-2 py-1.5" /></label>{!validation.valid && <span className="text-xs text-red-600">{validation.message}</span>}{graphCustomized && <span className="text-xs text-indigo-600">当前将保存完整规则图中的修改。</span>}</div>
            {showGraph && <div className="mt-4 h-[520px] overflow-hidden rounded-xl border border-white/70"><JdmConfigProvider><DndProvider backend={HTML5Backend}><DecisionGraph value={graph} onChange={(value) => { setGraph(value as DecisionGraphType); setGraphCustomized(true); setSimulation(null) }} mode="dev" /></DndProvider></JdmConfigProvider></div>}
          </section>

          <section className="neu-card p-4" aria-labelledby="verification-heading">
            <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 id="verification-heading" className="text-sm font-bold text-gray-800">3. 试算、发布和启用</h3><p className="mt-1 text-xs text-gray-500">试算不下发；发布冻结版本；启用后才会在整分钟产生控制意图。</p></div><div className="flex flex-wrap gap-2"><button type="button" onClick={save} disabled={!!busy} className="neu-btn px-3 py-1.5 text-xs">保存草稿</button><button type="button" onClick={simulate} disabled={!!busy} className="neu-btn px-3 py-1.5 text-xs text-indigo-700">试算</button><button type="button" onClick={publish} disabled={!!busy} className="neu-btn px-3 py-1.5 text-xs text-[#287c12]">发布</button>{strategy.enabled ? <button type="button" onClick={disable} disabled={!!busy} className="neu-btn px-3 py-1.5 text-xs text-red-600">停用</button> : <button type="button" onClick={enable} disabled={!!busy || !strategy.published_revision} className="rounded-lg bg-[#52c41a] px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-40">启用</button>}{strategy.runtime_health === 'FAILED' && <button type="button" onClick={clearFailure} disabled={!!busy} className="neu-btn px-3 py-1.5 text-xs text-red-600">清除故障锁</button>}</div></div>
            {simulation && <div className="mt-4 grid gap-3 md:grid-cols-4" data-testid="strategy-simulation"><div className="neu-inset p-3"><div className="text-[10px] text-gray-500">试算状态</div><div className="mt-1 text-xs font-semibold">{simulation.status}{simulation.reason_code ? ` · ${simulation.reason_code}` : ''}</div></div><div className="neu-inset p-3"><div className="text-[10px] text-gray-500">快照证据</div><div className="mt-1 text-xs">帧 {simulation.frame_sequence ?? '—'} · 配置 {simulation.configuration_revision ?? '—'} · {Object.keys(simulation.snapshot).length} 个实体</div></div><div className="neu-inset p-3"><div className="text-[10px] text-gray-500">命中行</div><div className="mt-1 text-xs font-semibold">{simulation.matched_rules.join('、') || '未命中'}</div></div><div className="neu-inset p-3"><div className="text-[10px] text-gray-500">拟执行意图</div><div className="mt-1 text-xs font-semibold">{simulation.proposed_intents.map((item) => `${item.action_id}=${valueText(item.value)}`).join('、') || '无需控制'}</div></div></div>}
          </section>

          <section className="neu-card p-4" aria-labelledby="events-heading"><div className="mb-3 flex items-center justify-between"><div><h3 id="events-heading" className="text-sm font-bold text-gray-800">4. 关键事件与控制回读</h3><p className="mt-1 text-xs text-gray-500">只保留有意义的变化、阻断、恢复和控制结果。</p></div><button type="button" onClick={() => selectedId && fetchDispatchStrategyEvents(selectedId, { limit: 30 }).then((page) => setEvents(page.items)).catch((reason) => setError(describeDispatchStrategyError(reason)))} className="neu-btn px-3 py-1.5 text-xs">刷新</button></div><div className="overflow-x-auto"><table className="w-full min-w-[760px] text-xs"><thead><tr className="border-b text-left text-gray-500"><th className="p-2">时间</th><th className="p-2">事件</th><th className="p-2">原因/命中</th><th className="p-2">控制命令</th><th className="p-2">回读状态</th></tr></thead><tbody>{events.map((event) => <tr key={event.id} className="border-b border-white/60"><td className="p-2">{new Date(event.occurred_at).toLocaleString()}</td><td className="p-2 font-medium">{event.event_kind}</td><td className="p-2">{event.reason_code || valueText(event.decision?.matched_rule)}</td><td className="p-2 font-mono text-[10px]">{event.control_command_id || '—'}</td><td className="p-2">{event.control_status || '—'}</td></tr>)}{!events.length && <tr><td colSpan={5} className="p-6 text-center text-gray-400">暂无关键事件</td></tr>}</tbody></table></div></section>
        </>}
      </main>
    </div>
  )
}
