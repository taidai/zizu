import { useEffect, useMemo, useState } from 'react'
import {
  fetchEmsWorkbench,
  fetchEmsWorkbenchTrend,
  submitControlCommand,
  type EmsWorkbench,
  type EmsWorkbenchTrend,
  type WorkbenchEntity,
} from '../api/client'

type WorkbenchTab = 'overview' | 'trends' | 'alarms' | 'controls'

interface Props {
  onOpenAlarms: () => void
}

function DisplayValue({ entity }: { entity: WorkbenchEntity }) {
  if (entity.status === 'unavailable') {
    return <span className="text-amber-600">数据不可用（{entity.code || 'UNKNOWN'}）</span>
  }
  return (
    <>
      <span className="font-mono-value text-lg font-semibold text-gray-800">{entity.value == null ? '—' : String(entity.value)}</span>
      {entity.unit && <span className="ml-1 text-xs text-gray-400">{entity.unit}</span>}
    </>
  )
}

function TrendChart({ trend }: { trend: EmsWorkbenchTrend | null }) {
  const points = useMemo(() => {
    if (!trend) return []
    return trend.series.flatMap((series) => series.points
      .filter((point): point is typeof point & { value: number } => typeof point.value === 'number')
      .map((point) => ({ ...point, name: series.display_name })))
  }, [trend])
  if (!trend) return <div className="py-12 text-center text-sm text-gray-400">正在加载趋势…</div>
  if (points.length === 0) return <div className="py-12 text-center text-sm text-gray-400">当前时间范围没有可用历史数据。</div>
  const values = points.map((point) => point.value)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const polyline = points.map((point, index) => {
    const x = points.length === 1 ? 50 : (index / (points.length - 1)) * 100
    const y = 92 - ((point.value - min) / span) * 80
    return `${x},${y}`
  }).join(' ')
  return (
    <div>
      <div className="mb-2 flex items-baseline justify-between text-xs text-gray-500">
        <span>{trend.label}</span><span>{min.toFixed(2)} — {max.toFixed(2)}</span>
      </div>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="h-56 w-full rounded-xl border border-white/60 bg-white/30 p-3">
        <line x1="0" x2="100" y1="92" y2="92" stroke="#d1d5db" strokeWidth="0.6" />
        <line x1="0" x2="100" y1="52" y2="52" stroke="#e5e7eb" strokeWidth="0.4" />
        <polyline points={polyline} fill="none" stroke="#52c41a" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      </svg>
      <p className="mt-2 text-[11px] text-gray-400">{points.length} 个来自已确认实体实例的采样点</p>
    </div>
  )
}

function Controls({ entities }: { entities: WorkbenchEntity[] }) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [message, setMessage] = useState('')
  const execute = async (entity: WorkbenchEntity) => {
    const raw = values[entity.entity_instance_id]
    if (raw == null || raw === '') return setMessage(`请先填写 ${entity.display_name} 的目标值。`)
    const value = entity.data_type === 'bool' ? raw === 'true' : ['float', 'int'].includes(entity.data_type) ? Number(raw) : raw
    if (typeof value === 'number' && !Number.isFinite(value)) return setMessage('目标值必须是有效数字。')
    try {
      const command = await submitControlCommand(entity.entity_instance_id, value)
      setMessage(`命令已受理：${command.id}（${command.status}）。请等待回读确认。`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '控制命令提交失败。')
    }
  }
  if (entities.length === 0) return <p className="text-sm text-gray-400">当前包没有可授权控制的实体。</p>
  return <div className="space-y-3">
    {entities.map((entity) => (
      <div key={entity.entity_instance_id} className="rounded-xl border border-white/60 bg-white/30 p-4 sm:flex sm:items-center sm:gap-4">
        <div className="min-w-0 flex-1"><div className="font-medium text-gray-700">{entity.display_name}</div><div className="mt-1 text-xs text-gray-400">{entity.instance_key} · {entity.definition_id}</div></div>
        {entity.data_type === 'bool' ? (
          <select value={values[entity.entity_instance_id] || ''} onChange={(event) => setValues({ ...values, [entity.entity_instance_id]: event.target.value })} className="neu-input mt-3 w-full px-3 py-2 text-sm sm:mt-0 sm:w-28"><option value="">选择</option><option value="true">开启</option><option value="false">关闭</option></select>
        ) : <input value={values[entity.entity_instance_id] || ''} onChange={(event) => setValues({ ...values, [entity.entity_instance_id]: event.target.value })} type={['float', 'int'].includes(entity.data_type) ? 'number' : 'text'} className="neu-input mt-3 w-full px-3 py-2 text-sm sm:mt-0 sm:w-36" placeholder={entity.unit || '目标值'} />}
        <button onClick={() => void execute(entity)} className="mt-3 w-full rounded-lg bg-[#52c41a] px-3 py-2 text-sm font-medium text-white sm:mt-0 sm:w-auto">提交命令</button>
      </div>
    ))}
    {message && <p role="status" className="text-xs text-gray-500">{message}</p>}
  </div>
}

export default function EMSWorkbenchPage({ onOpenAlarms }: Props) {
  const [workbench, setWorkbench] = useState<EmsWorkbench | null>(null)
  const [activeTab, setActiveTab] = useState<WorkbenchTab>('overview')
  const [selectedTrend, setSelectedTrend] = useState<string>('')
  const [trend, setTrend] = useState<EmsWorkbenchTrend | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    fetchEmsWorkbench().then((payload) => {
      setWorkbench(payload)
      setActiveTab(payload.navigation[0]?.id || 'overview')
      setSelectedTrend(payload.trends[0]?.id || '')
    }).catch((reason) => setError(reason instanceof Error ? reason.message : '无法加载 EMS 工作台。'))
  }, [])

  const selected = workbench?.trends.find((item) => item.id === selectedTrend)
  useEffect(() => {
    if (!selected) return
    setTrend(null)
    fetchEmsWorkbenchTrend(selected.id, selected.default_range).then(setTrend).catch((reason) => setError(reason instanceof Error ? reason.message : '无法加载趋势。'))
  }, [selected?.id, selected?.default_range])

  if (error) return <div role="alert" className="neu-card p-6 text-sm text-red-700">EMS 工作台不可用：{error}</div>
  if (!workbench) return <div className="neu-card p-6 text-sm text-gray-400">正在加载 EMS 工作台...</div>
  const navigation = workbench.navigation
  return (
    <section className="space-y-4">
      <div className="neu-card p-5">
        <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-xl font-bold text-gray-800">EMS 运行工作台</h2><p className="mt-1 text-xs text-gray-400">配置版本 {workbench.site_configuration_version} · {workbench.workbench_id}</p></div></div>
        <div className="mt-5 flex flex-wrap gap-2">{navigation.map((item) => <button key={item.id} onClick={() => item.id === 'alarms' ? onOpenAlarms() : setActiveTab(item.id)} className={`rounded-lg px-3 py-2 text-sm ${activeTab === item.id ? 'bg-[#52c41a] text-white' : 'neu-btn text-gray-600'}`}>{item.label}</button>)}</div>
      </div>
      {activeTab === 'overview' && <><div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{workbench.kpis.map((kpi) => <div key={kpi.id} className="neu-card p-5"><div className="text-xs text-gray-400">{kpi.label}</div><div className="mt-3"><DisplayValue entity={kpi.entities[0]} /></div></div>)}</div><div className="grid gap-4 lg:grid-cols-2">{workbench.groups.map((group) => <div key={group.id} className="neu-card p-5"><h3 className="font-semibold text-gray-700">{group.label}</h3><div className="mt-3 space-y-3">{group.entities.map((entity) => <div key={entity.entity_instance_id} className="flex items-center justify-between border-t border-white/60 pt-3"><span className="text-sm text-gray-600">{entity.display_name}</span><DisplayValue entity={entity} /></div>)}</div></div>)}</div></>}
      {activeTab === 'trends' && <div className="neu-card p-5"><div className="mb-4 flex flex-wrap gap-2">{workbench.trends.map((item) => <button key={item.id} onClick={() => setSelectedTrend(item.id)} className={`rounded-lg px-3 py-2 text-sm ${selectedTrend === item.id ? 'bg-[#52c41a] text-white' : 'neu-btn text-gray-600'}`}>{item.label}</button>)}</div><TrendChart trend={trend} /></div>}
      {activeTab === 'controls' && <div className="neu-card p-5"><h3 className="mb-4 font-semibold text-gray-700">授权控制</h3>{workbench.controls.visible ? <Controls entities={workbench.controls.entities} /> : <p className="text-sm text-gray-400">本解决方案包未启用控制入口。</p>}</div>}
    </section>
  )
}
