import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch, archiveAlarm, fetchAlarms, acknowledgeAlarm, fetchAlarmEntities, type Alarm, type AlarmLevel } from '../api/client'
import MinimalAlarmRulesPage from './MinimalAlarmRulesPage'
import AlarmNotificationRecords from '../components/alarm-center/AlarmNotificationRecords'
import { acknowledgeAlarmBatch, canArchiveAlarmEvent, currentAlarmBatchIds, pruneCurrentAlarmSelection, updateCurrentAlarmSelection } from '../components/alarm-center/alarmCenterModel'
import '../components/alarm-center/tabletApplications.css'

const LEVEL_STYLES: Record<AlarmLevel, string> = {
  CRITICAL: 'bg-red-100 text-red-700 border-red-200',
  MAJOR: 'bg-orange-100 text-orange-700 border-orange-200',
  WARNING: 'bg-amber-100 text-amber-700 border-amber-200',
  INFO: 'bg-blue-100 text-blue-700 border-blue-200',
}

interface Stats {
  active: number
  unack: number
  critical: number
}

interface AlarmEventDetail {
  id: string
  definition_id: string
  entity_instance_id: string
  state: string
  severity: AlarmLevel
  pending_at: string
  active_at: string | null
  acknowledged_at: string | null
  acknowledged_by: string | null
  acknowledgement_note?: string | null
  recovered_at: string | null
  node_name: string
  entity_name: string
  alarm_name: string
  duration_seconds: number
  archived_at: string | null
  archived_by: string | null
}

interface AlarmTransition {
  id: string
  from_state: string | null
  to_state: string
  occurred_at: string
  code: string
  evidence?: Record<string, unknown> | null
  actor?: string | null
  note?: string | null
}

type AlarmEventView = 'current' | 'history'
type AlarmStatusFilter = 'active' | 'acknowledged' | 'resolved' | 'archived'

function AlarmEventTable({ canArchive, view }: { canArchive: boolean; view: AlarmEventView }) {
  const historical = view === 'history'
  const [alarms, setAlarms] = useState<Alarm[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [listError, setListError] = useState(false)
  const pendingRequest = useRef<AbortController | null>(null)
  const detailRequest = useRef<AbortController | null>(null)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const [levelFilter, setLevelFilter] = useState<AlarmLevel | ''>('')
  const [entityFilter, setEntityFilter] = useState<string>('')
  const [alarmEntities, setAlarmEntities] = useState<{ id: string; name: string; display_name: string | null }[]>([])
  const [statusFilter, setStatusFilter] = useState<AlarmStatusFilter>(historical ? 'resolved' : 'active')
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [stats, setStats] = useState<Stats>({ active: 0, unack: 0, critical: 0 })
  const [pageSize, setPageSize] = useState<10 | 20>(10)
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [batchMessage, setBatchMessage] = useState('')
  const [detail, setDetail] = useState<AlarmEventDetail | null>(null)
  const [transitions, setTransitions] = useState<AlarmTransition[]>([])
  const [detailOpen, setDetailOpen] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const scopeGenerationRef = useRef(0)
  const scopeIdentityRef = useRef<string | null>(null)
  const scopeIdentity = [page, pageSize, levelFilter, entityFilter, statusFilter].join('\u0001')
  if (scopeIdentityRef.current !== scopeIdentity) {
    scopeIdentityRef.current = scopeIdentity
    scopeGenerationRef.current += 1
  }

  const load = useCallback(async (background = false) => {
    // A slow refresh must not pile up more requests or erase the last result.
    if (background && pendingRequest.current) return
    pendingRequest.current?.abort()
    const request = new AbortController()
    pendingRequest.current = request
    setLoading(true)
    setError('')
    setListError(false)
    if (!background) {
      setAlarms([])
      setSelectedIds([])
    }
    try {
      const level = levelFilter || undefined
      const entityId = entityFilter || undefined
      const acknowledged = statusFilter === 'acknowledged' ? true : statusFilter === 'active' ? false : undefined
      const resolved = statusFilter === 'resolved' ? true : statusFilter === 'active' ? false : undefined
      const data = await fetchAlarms(
        page, pageSize, level, undefined, acknowledged, resolved, undefined,
        entityId, request.signal, statusFilter === 'archived' ? 'archived' : undefined,
      )
      if (request.signal.aborted || pendingRequest.current !== request) return
      setAlarms(data.alarms)
      setSelectedIds((current) => historical
        ? current.filter((id) => data.alarms.some((alarm) => alarm.id === id && canArchiveAlarmEvent(alarm)))
        : pruneCurrentAlarmSelection(current, data.alarms))
      setListError(false)
      setTotalPages(data.total_pages || 1)
      setStats({
        active: data.summary.active,
        unack: data.summary.unacknowledged,
        critical: data.summary.critical,
      })
    } catch {
      if (!request.signal.aborted && pendingRequest.current === request) {
        setListError(true)
        setError(background ? '刷新失败，当前保留上次结果。' : '告警加载失败，请重试。')
      }
    } finally {
      if (pendingRequest.current === request) {
        pendingRequest.current = null
        setLoading(false)
      }
    }
  }, [page, pageSize, levelFilter, entityFilter, statusFilter, historical])

  useEffect(() => {
    fetchAlarmEntities().then((d) => setAlarmEntities(d.items)).catch(() => {})
  }, [])

  useEffect(() => () => detailRequest.current?.abort(), [])

  useEffect(() => {
    void load()
    return () => {
      pendingRequest.current?.abort()
      pendingRequest.current = null
    }
  }, [load])

  useEffect(() => {
    if (!autoRefresh) return
    const id = setInterval(() => { void load(true) }, 5000)
    return () => clearInterval(id)
  }, [autoRefresh, load])

  const handleAckIds = async (ids: string[]) => {
    if (!ids.length) return
    const acknowledgementScopeGeneration = scopeGenerationRef.current
    setBatchMessage('')
    const result = await acknowledgeAlarmBatch(ids, acknowledgeAlarm)
    if (acknowledgementScopeGeneration !== scopeGenerationRef.current) return
    setSelectedIds([])
    setBatchMessage(result.failures.length
      ? `部分确认未完成：${result.failures.map((item) => `${item.id}（${item.message}）`).join('；')}`
      : `已确认 ${result.succeededIds.length} 条告警。`)
    await load()
  }

  const handleSelectedAck = () => {
    const ids = currentAlarmBatchIds(selectedIds, alarms, {
      loading: loading || pendingRequest.current !== null,
      error: listError,
    })
    if (ids.length) void handleAckIds(ids)
  }

  const clearAnd = (operation: () => void) => {
    setSelectedIds([])
    setBatchMessage('')
    operation()
  }

  const openDetail = async (alarm: Alarm) => {
    detailRequest.current?.abort()
    const request = new AbortController()
    detailRequest.current = request
    setDetail(null)
    setTransitions([])
    setDetailOpen(true)
    setDetailLoading(true)
    setDetailError('')
    try {
      const [eventResponse, timelineResponse] = await Promise.all([
        apiFetch(`/api/v1/alarm-events/${encodeURIComponent(alarm.id)}`, { signal: request.signal }),
        apiFetch(`/api/v1/alarm-events/${encodeURIComponent(alarm.id)}/transitions`, { signal: request.signal }),
      ])
      if (!eventResponse.ok || !timelineResponse.ok) throw new Error('detail unavailable')
      const [event, timeline] = await Promise.all([eventResponse.json(), timelineResponse.json()]) as [AlarmEventDetail, { items: AlarmTransition[] }]
      if (request.signal.aborted || detailRequest.current !== request) return
      setDetail(event)
      setTransitions(timeline.items)
    } catch {
      if (!request.signal.aborted && detailRequest.current === request) setDetailError('告警详情加载失败，请关闭后重试。')
    } finally {
      if (detailRequest.current === request) {
        detailRequest.current = null
        setDetailLoading(false)
      }
    }
  }

  const handleArchive = async (alarm: Alarm) => {
    if (!window.confirm(`确定归档“${alarm.message}”吗？\n记录和发送证据仍会保留。`)) return
    try {
      await archiveAlarm(alarm.id)
      void load()
    } catch {
      alert('归档失败，请确认该告警已经现场恢复。')
    }
  }

  const archiveableIds = canArchive
    ? alarms.filter((alarm) => canArchiveAlarmEvent(alarm)).map((alarm) => alarm.id)
    : []
  const selectedBatchIds = historical
    ? (loading || listError ? [] : selectedIds.filter((id) => archiveableIds.includes(id)))
    : currentAlarmBatchIds(selectedIds, alarms, { loading, error: listError })

  const handleSelectedArchive = async () => {
    if (!selectedBatchIds.length || !window.confirm(`确定归档选中的 ${selectedBatchIds.length} 条已恢复告警吗？\n记录和发送证据仍会保留。`)) return
    const archiveScopeGeneration = scopeGenerationRef.current
    setBatchMessage('')
    const result = await acknowledgeAlarmBatch(selectedBatchIds, archiveAlarm)
    if (archiveScopeGeneration !== scopeGenerationRef.current) return
    setSelectedIds([])
    setBatchMessage(result.failures.length
      ? `部分归档未完成：${result.failures.map((item) => `${item.id}（${item.message}）`).join('；')}`
      : `已归档 ${result.succeededIds.length} 条告警。`)
    await load()
  }

  const selectableIds = historical
    ? archiveableIds
    : alarms.filter((alarm) => alarm.state === 'active_unacknowledged').map((alarm) => alarm.id)
  const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selectedIds.includes(id))
  const toggleCurrentPage = (checked: boolean) => setSelectedIds(checked ? selectableIds : [])

  return (
    <div className="space-y-3">
      {/* 统计卡 */}
      {!historical && <div className="neu-card flex flex-wrap items-center gap-x-6 gap-y-2 px-3 py-2 text-xs text-gray-500">
        <span>活动告警 <strong className="ml-1 font-mono-value text-base text-gray-800">{stats.active}</strong></span>
        <span>待确认 <strong className="ml-1 font-mono-value text-base text-red-600">{stats.unack}</strong></span>
        <button onClick={() => clearAnd(() => { setPage(1); setLevelFilter(levelFilter === 'CRITICAL' ? '' : 'CRITICAL') })} className={`rounded px-2 text-left transition ${levelFilter === 'CRITICAL' ? 'bg-red-50 ring-2 ring-red-300' : ''}`}>紧急 <strong className="ml-1 font-mono-value text-base text-red-600">{stats.critical}</strong></button>
        <label className="ml-auto flex cursor-pointer items-center gap-2 text-xs text-gray-600"><input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} className="h-4 w-4 accent-[#52c41a]" />自动刷新 (5s)</label>
      </div>}

      {/* 筛选栏 */}
      <div className="neu-card flex flex-wrap items-center gap-2 p-3">
          <span className="text-xs text-gray-500">状态:</span>
          {(historical ? [
            { key: 'resolved', label: '已恢复' },
            { key: 'archived', label: '已归档' },
          ] : [
            { key: 'active', label: '未恢复' },
            { key: 'acknowledged', label: '已确认' },
          ]).map((s) => (
            <button
              key={s.key}
              onClick={() => clearAnd(() => { setPage(1); setStatusFilter(s.key as AlarmStatusFilter) })}
              className={`neu-btn px-3 py-1 text-xs ${statusFilter === s.key ? 'zizu-tab-active' : 'text-gray-600'}`}
            >
              {s.label}
            </button>
          ))}
          <span aria-hidden="true" className="mx-1 h-6 w-px bg-white/80" />
          <span className="text-xs text-gray-500">实体:</span>
          <select
            value={entityFilter}
            onChange={(e) => clearAnd(() => { setPage(1); setEntityFilter(e.target.value) })}
            className="neu-input text-xs px-2 py-1 bg-white border border-gray-200 rounded"
          >
            <option value="">全部实体</option>
            {alarmEntities.map((ent) => (
              <option key={ent.id} value={ent.id}>{ent.display_name || ent.name}</option>
            ))}
          </select>
          {entityFilter && (
            <button
              onClick={() => clearAnd(() => { setPage(1); setEntityFilter('') })}
              className="text-[10px] text-gray-400 hover:text-gray-600"
            >
              清除
            </button>
          )}
      </div>

      {/* 告警事件表 */}
      <div className="neu-card overflow-hidden" data-testid="alarm-event-table">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/70 p-3">
          <div className="flex items-center gap-2 text-xs text-gray-600">
            {historical
              ? <button type="button" disabled={!selectedBatchIds.length} onClick={() => void handleSelectedArchive()} className="neu-btn px-3 py-2 font-semibold text-[#981320] disabled:opacity-40">归档所选（{selectedBatchIds.length}）</button>
              : <button type="button" disabled={!selectedBatchIds.length} onClick={handleSelectedAck} className="neu-btn zizu-primary px-3 py-2 font-semibold disabled:opacity-40">确认所选（{selectedBatchIds.length}）</button>}
            {loading && <span>{alarms.length ? '更新中…' : '加载中…'}</span>}
            {historical && !canArchive && <span className="text-gray-500">当前账户仅可查看历史事件。</span>}
          </div>
          <label className="text-xs text-gray-600">每页
            <select aria-label={historical ? '历史记录每页条数' : '每页条数'} value={pageSize} onChange={(event) => clearAnd(() => { setPage(1); setPageSize(Number(event.target.value) as 10 | 20) })} className="neu-input mx-2 px-2 py-1.5">
              <option value={10}>10</option><option value={20}>20</option>
            </select>条
          </label>
        </div>
        {error && <div role="alert" className="border-b border-red-100 bg-red-50 p-3 text-xs text-red-700">{error} <button onClick={() => void load()} className="underline">重试</button></div>}
        {batchMessage && <div role="status" className={`border-b p-3 text-xs ${batchMessage.startsWith('部分') ? 'border-amber-100 bg-amber-50 text-amber-800' : 'border-green-100 bg-green-50 text-green-700'}`}>{batchMessage}</div>}
        <div className="alarm-table-viewport">
          <table className="alarm-event-table w-full table-fixed text-xs" data-testid={historical ? 'alarm-history-table' : 'alarm-current-table'}>
            <thead><tr className="border-b border-white/70 text-left text-gray-500">
              <th className="w-10 p-3"><input aria-label={historical ? '选择当前页可归档告警' : '选择当前页可确认告警'} type="checkbox" disabled={!selectableIds.length} checked={allSelected} onChange={(event) => historical ? toggleCurrentPage(event.target.checked) : setSelectedIds((current) => updateCurrentAlarmSelection(current, alarms, event.target.checked))} /></th>
              <th className="w-20 p-3">等级</th><th className="p-3">告警 / 实体</th><th data-alarm-column="secondary" className="w-28 p-3">节点</th><th className="w-40 p-3">{historical ? '恢复时间' : '触发时间'}</th><th data-alarm-column="secondary" className="w-16 p-3">持续</th><th className="w-24 p-3">状态</th><th className="w-48 p-3">操作</th>
            </tr></thead>
            <tbody>{alarms.map((alarm) => <tr key={alarm.id} tabIndex={0} onClick={() => void openDetail(alarm)} onKeyDown={(event) => { if (event.key === 'Enter') void openDetail(alarm) }} className={`cursor-pointer border-b border-white/60 hover:bg-white/40 ${alarm.level === 'CRITICAL' ? 'border-l-4 border-l-red-500' : ''}`}>
              <td className="p-3" onClick={(event) => event.stopPropagation()}><input aria-label={`选择告警 ${alarm.id}`} type="checkbox" disabled={!selectableIds.includes(alarm.id)} checked={selectedIds.includes(alarm.id)} onChange={(event) => setSelectedIds((current) => event.target.checked ? [...new Set([...current, alarm.id])] : current.filter((id) => id !== alarm.id))} /></td>
              <td className="p-3"><span className={`rounded border px-2 py-1 text-[10px] font-bold ${LEVEL_STYLES[alarm.level]}`}>{alarm.level}</span></td>
              <td className="p-3"><h3 className="truncate font-semibold text-gray-800" title={alarm.message}>{alarm.message}</h3><div className="mt-1 truncate text-gray-500" title={alarm.entity_name || alarm.entity_id || ''}>{alarm.entity_name || alarm.entity_id || '—'}</div></td>
              <td data-alarm-column="secondary" className="truncate p-3">{alarm.node_name || '—'}</td><td className="p-3">{new Date(historical && alarm.resolved_at ? alarm.resolved_at : alarm.created_at).toLocaleString('zh-CN', { hour12: false })}</td><td data-alarm-column="secondary" className="p-3">{alarm.duration_seconds ?? 0}s</td>
              <td className="p-3">{alarm.state === 'pending' ? '触发待确认' : alarm.state === 'active_unacknowledged' ? '活动未确认' : alarm.state === 'active_acknowledged' ? '活动已确认' : alarm.archived_at ? '已归档' : '已恢复'}</td>
              <td className="p-3" onClick={(event) => event.stopPropagation()}><div className="flex justify-end gap-2"><button type="button" onClick={() => void openDetail(alarm)} className="neu-btn px-3 py-1.5 text-gray-600">详情</button>{alarm.state === 'active_unacknowledged' && <button type="button" onClick={() => void handleAckIds([alarm.id])} className="neu-btn zizu-primary px-3 py-1.5 font-medium">确认</button>}{canArchive && canArchiveAlarmEvent(alarm) && <button type="button" onClick={() => void handleArchive(alarm)} className="neu-btn px-3 py-1.5 text-gray-600">归档</button>}</div></td>
            </tr>)}{!alarms.length && !loading && !error && <tr><td colSpan={8} className="p-8 text-center text-sm text-gray-400">当前筛选条件下无告警</td></tr>}</tbody>
          </table>
        </div>
      </div>

      {/* 分页 */}
      {totalPages > 1 && (
        <div className="flex justify-center items-center gap-2 text-xs text-gray-500">
          <button
            onClick={() => clearAnd(() => setPage((p) => Math.max(1, p - 1)))}
            disabled={page <= 1}
            className="neu-btn w-7 h-7 flex items-center justify-center disabled:opacity-30"
          >
            ‹
          </button>
          <span className="px-2 font-mono">{page} / {totalPages}</span>
          <button
            onClick={() => clearAnd(() => setPage((p) => Math.min(totalPages, p + 1)))}
            disabled={page >= totalPages}
            className="neu-btn w-7 h-7 flex items-center justify-center disabled:opacity-30"
          >
            ›
          </button>
        </div>
      )}

      {detailOpen && <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 p-4" role="dialog" aria-modal="true" aria-label="告警详情">
        <div className="neu-card tablet-alarm-dialog max-h-[90vh] w-full max-w-3xl overflow-y-auto p-5">
          <div className="flex items-start justify-between gap-3"><div><h3 className="text-base font-bold text-gray-800">告警详情</h3><p className="mt-1 font-mono text-[10px] text-gray-400">{detail?.id}</p></div><button type="button" onClick={() => { detailRequest.current?.abort(); detailRequest.current = null; setDetailOpen(false); setDetailLoading(false); setDetail(null); setTransitions([]); setDetailError('') }} className="neu-btn px-3 py-2 text-xs">关闭</button></div>
          {detailLoading ? <p role="status" className="py-10 text-center text-sm text-gray-500">正在读取事件和流转证据…</p> : detailError ? <p role="alert" className="mt-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">{detailError}</p> : detail && <div className="mt-4 space-y-4 text-xs">
            <dl className="grid gap-3 sm:grid-cols-2"><div><dt className="text-gray-400">告警 / 等级</dt><dd className="mt-1 font-semibold">{detail.alarm_name} · {detail.severity}</dd></div><div><dt className="text-gray-400">状态</dt><dd className="mt-1">{detail.state}</dd></div><div><dt className="text-gray-400">节点 / 实体</dt><dd className="mt-1">{detail.node_name} / {detail.entity_name}</dd></div><div><dt className="text-gray-400">定义 / 实例</dt><dd className="mt-1 break-all font-mono text-[10px]">{detail.definition_id}<br />{detail.entity_instance_id}</dd></div><div><dt className="text-gray-400">触发 / 恢复</dt><dd className="mt-1">{detail.active_at || detail.pending_at}<br />{detail.recovered_at || '尚未恢复'}</dd></div><div><dt className="text-gray-400">确认记录</dt><dd className="mt-1">{detail.acknowledged_at ? `${detail.acknowledged_by || '未知'} · ${detail.acknowledged_at}${detail.acknowledgement_note ? ` · ${detail.acknowledgement_note}` : ''}` : '尚未确认'}</dd></div></dl>
            <div><h4 className="font-semibold text-gray-700">状态流转与证据</h4><div className="mt-2 space-y-2">{transitions.map((item) => <div key={item.id} className="neu-inset p-3"><div className="flex flex-wrap justify-between gap-2"><span className="font-semibold">{item.code} · {item.from_state || '开始'} → {item.to_state}</span><span>{item.occurred_at}</span></div>{(item.actor || item.note) && <p className="mt-1 text-gray-500">{item.actor || '系统'}{item.note ? ` · ${item.note}` : ''}</p>}{item.evidence && <pre className="mt-2 whitespace-pre-wrap break-all rounded bg-white/50 p-2 font-mono text-[10px]">{JSON.stringify(item.evidence, null, 2)}</pre>}</div>)}{!transitions.length && <p className="text-gray-400">暂无状态流转。</p>}</div></div>
          </div>}
        </div>
      </div>}
    </div>
  )
}

export default function AlarmCenterPage({ actorId, canConfigure }: { actorId: string; canConfigure: boolean }) {
  const [tab, setTab] = useState<'current' | 'history' | 'notifications'>('current')
  const [rulesOpen, setRulesOpen] = useState(false)
  return <div className="tablet-applications tablet-alarm-shell space-y-3" data-testid="tablet-alarm-applications">
    <header className="flex flex-wrap items-end justify-between gap-3">
      <div><h1 className="text-lg font-bold text-gray-800">告警中心</h1><p className="mt-1 text-xs text-gray-500">统一查看真实告警事件、历史证据与 HTTP 通知投递。</p></div>
      {canConfigure
        ? <button type="button" onClick={() => setRulesOpen(true)} className="neu-btn px-4 py-2 text-sm font-semibold text-[#981320]">告警规则</button>
        : <span className="text-xs text-gray-500">规则配置需要实施工程师或管理员权限。</span>}
    </header>
    <div className="flex gap-2 border-b border-white/70 pb-2" aria-label="告警记录视图">
      <button type="button" aria-pressed={tab === 'current'} onClick={() => setTab('current')} className={`rounded-lg border border-transparent px-4 py-2 text-sm font-semibold ${tab === 'current' ? 'zizu-tab-active' : 'text-gray-600'}`}>当前告警</button>
      <button type="button" aria-pressed={tab === 'history'} onClick={() => setTab('history')} className={`rounded-lg border border-transparent px-4 py-2 text-sm font-semibold ${tab === 'history' ? 'zizu-tab-active' : 'text-gray-600'}`}>历史记录</button>
      <button type="button" aria-pressed={tab === 'notifications'} onClick={() => setTab('notifications')} className={`rounded-lg border border-transparent px-4 py-2 text-sm font-semibold ${tab === 'notifications' ? 'zizu-tab-active' : 'text-gray-600'}`}>通知记录</button>
    </div>
    {tab === 'current' && <AlarmEventTable key="current" canArchive={canConfigure} view="current" />}
    {tab === 'history' && <AlarmEventTable key="history" canArchive={canConfigure} view="history" />}
    {tab === 'notifications' && <AlarmNotificationRecords canManage={canConfigure} />}
    {rulesOpen && canConfigure && <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/35 p-3" role="dialog" aria-modal="true" aria-label="告警规则配置">
      <div className="neu-card tablet-alarm-rule-dialog flex max-h-[94vh] w-full max-w-6xl flex-col overflow-hidden p-4">
        <div className="flex items-start justify-between gap-3 border-b border-white/70 pb-3"><div><h2 className="text-base font-bold text-gray-800">告警规则配置</h2><p className="mt-1 text-xs text-gray-500">试算和计划不会改变运行态；确认发布后才推进统一配置修订。</p></div><button type="button" onClick={() => setRulesOpen(false)} className="neu-btn px-3 py-2 text-xs">关闭</button></div>
        <div className="min-h-0 flex-1 overflow-y-auto pt-3"><MinimalAlarmRulesPage key={actorId} /></div>
      </div>
    </div>}
  </div>
}
