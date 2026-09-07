import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch, archiveAlarm, fetchAlarms, acknowledgeAlarm, fetchAlarmEntities, type Alarm, type AlarmLevel } from '../api/client'
import MinimalAlarmRulesPage from './MinimalAlarmRulesPage'
import AlarmNotificationRecords from '../components/alarm-center/AlarmNotificationRecords'
import { acknowledgeAlarmBatch, canArchiveAlarmEvent, updateCurrentAlarmSelection } from '../components/alarm-center/alarmCenterModel'
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

function CurrentAlarmView({ canArchive }: { canArchive: boolean }) {
  const [alarms, setAlarms] = useState<Alarm[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const pendingRequest = useRef<AbortController | null>(null)
  const detailRequest = useRef<AbortController | null>(null)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const [levelFilter, setLevelFilter] = useState<AlarmLevel | ''>('')
  const [entityFilter, setEntityFilter] = useState<string>('')
  const [alarmEntities, setAlarmEntities] = useState<{ id: string; name: string; display_name: string | null }[]>([])
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'acknowledged' | 'resolved' | 'archived'>('active')
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [stats, setStats] = useState<Stats>({ active: 0, unack: 0, critical: 0 })
  const [pageSize, setPageSize] = useState<10 | 20>(10)
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [batchMessage, setBatchMessage] = useState('')
  const [detail, setDetail] = useState<AlarmEventDetail | null>(null)
  const [transitions, setTransitions] = useState<AlarmTransition[]>([])
  const [detailLoading, setDetailLoading] = useState(false)

  const load = useCallback(async (background = false) => {
    // A slow refresh must not pile up more requests or erase the last result.
    if (background && pendingRequest.current) return
    pendingRequest.current?.abort()
    const request = new AbortController()
    pendingRequest.current = request
    setLoading(true)
    setError('')
    if (!background) setAlarms([])
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
      setTotalPages(data.total_pages || 1)
      setStats({
        active: data.summary.active,
        unack: data.summary.unacknowledged,
        critical: data.summary.critical,
      })
    } catch {
      if (!request.signal.aborted && pendingRequest.current === request) {
        setError(background ? '刷新失败，当前保留上次结果。' : '告警加载失败，请重试。')
      }
    } finally {
      if (pendingRequest.current === request) {
        pendingRequest.current = null
        setLoading(false)
      }
    }
  }, [page, pageSize, levelFilter, entityFilter, statusFilter])

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
    setBatchMessage('')
    const result = await acknowledgeAlarmBatch(ids, acknowledgeAlarm)
    setSelectedIds([])
    setBatchMessage(result.failures.length
      ? `部分确认未完成：${result.failures.map((item) => `${item.id}（${item.message}）`).join('；')}`
      : `已确认 ${result.succeededIds.length} 条告警。`)
    await load()
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
    setDetailLoading(true)
    setError('')
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
      if (!request.signal.aborted && detailRequest.current === request) setError('告警详情加载失败，请重试。')
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

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-bold text-gray-800">告警中心</h2>
          <p className="text-xs text-gray-500">查看统一告警事件；确认表示已知悉，只有现场恢复条件才能关闭事件。</p>
        </div>
        <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
            className="w-4 h-4 accent-[#52c41a]"
          />
          自动刷新 (5s)
        </label>
      </div>

      {/* 统计卡 */}
      <div className="grid grid-cols-3 gap-3">
        <div className="neu-card p-3">
          <div className="text-[10px] text-gray-400 uppercase">活动告警</div>
          <div className="text-lg font-bold text-gray-800 font-mono-value">{stats.active}</div>
        </div>
        <div className="neu-card p-3">
          <div className="text-[10px] text-gray-400 uppercase">未确认</div>
          <div className="text-lg font-bold text-gray-800 font-mono-value">{stats.unack}</div>
        </div>
        <button onClick={() => clearAnd(() => { setPage(1); setLevelFilter(levelFilter === 'CRITICAL' ? '' : 'CRITICAL') })} className={`neu-card p-3 text-left transition ${levelFilter === 'CRITICAL' ? 'ring-2 ring-red-400' : ''}`}>
          <div className="text-[10px] text-gray-400 uppercase">紧急</div>
          <div className="text-lg font-bold text-red-600 font-mono-value">{stats.critical}</div>
        </button>
      </div>

      {/* 筛选栏 */}
      <div className="neu-card p-3 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-gray-500">状态:</span>
          {[
            { key: 'active', label: '未恢复' },
            { key: 'acknowledged', label: '已确认' },
            { key: 'resolved', label: '已恢复' },
            { key: 'archived', label: '已归档' },
            { key: 'all', label: '全部' },
          ].map((s) => (
            <button
              key={s.key}
              onClick={() => clearAnd(() => { setPage(1); setStatusFilter(s.key as typeof statusFilter) })}
              className={`neu-btn px-3 py-1 text-xs ${statusFilter === s.key ? 'zizu-tab-active' : 'text-gray-600'}`}
            >
              {s.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2">
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
      </div>

      {/* 告警事件表 */}
      <div className="neu-card overflow-hidden" data-testid="alarm-event-table">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/70 p-3">
          <div className="flex items-center gap-2 text-xs text-gray-600">
            <button type="button" disabled={!selectedIds.length} onClick={() => void handleAckIds(selectedIds)} className="neu-btn zizu-primary px-3 py-2 font-semibold disabled:opacity-40">确认所选（{selectedIds.length}）</button>
            {loading && <span>{alarms.length ? '更新中…' : '加载中…'}</span>}
          </div>
          <label className="text-xs text-gray-600">每页
            <select aria-label="每页条数" value={pageSize} onChange={(event) => clearAnd(() => { setPage(1); setPageSize(Number(event.target.value) as 10 | 20) })} className="neu-input mx-2 px-2 py-1.5">
              <option value={10}>10</option><option value={20}>20</option>
            </select>条
          </label>
        </div>
        {error && <div role="alert" className="border-b border-red-100 bg-red-50 p-3 text-xs text-red-700">{error} <button onClick={() => void load()} className="underline">重试</button></div>}
        {batchMessage && <div role="status" className={`border-b p-3 text-xs ${batchMessage.startsWith('部分') ? 'border-amber-100 bg-amber-50 text-amber-800' : 'border-green-100 bg-green-50 text-green-700'}`}>{batchMessage}</div>}
        <div className="overflow-x-auto">
          <table className="w-full min-w-[850px] text-xs">
            <thead><tr className="border-b border-white/70 text-left text-gray-500">
              <th className="p-3"><input aria-label="选择当前页可确认告警" type="checkbox" checked={alarms.some((item) => item.state === 'active_unacknowledged') && alarms.filter((item) => item.state === 'active_unacknowledged').every((item) => selectedIds.includes(item.id))} onChange={(event) => setSelectedIds((current) => updateCurrentAlarmSelection(current, alarms, event.target.checked))} /></th>
              <th className="p-3">等级</th><th className="p-3">告警 / 实体</th><th className="p-3">节点</th><th className="p-3">触发时间</th><th className="p-3">持续</th><th className="p-3">状态</th><th className="p-3">操作</th>
            </tr></thead>
            <tbody>{alarms.map((alarm) => <tr key={alarm.id} tabIndex={0} onClick={() => void openDetail(alarm)} onKeyDown={(event) => { if (event.key === 'Enter') void openDetail(alarm) }} className={`cursor-pointer border-b border-white/60 hover:bg-white/40 ${alarm.level === 'CRITICAL' ? 'border-l-4 border-l-red-500' : ''}`}>
              <td className="p-3" onClick={(event) => event.stopPropagation()}><input aria-label={`选择告警 ${alarm.id}`} type="checkbox" disabled={alarm.state !== 'active_unacknowledged'} checked={selectedIds.includes(alarm.id)} onChange={(event) => setSelectedIds((current) => event.target.checked ? [...new Set([...current, alarm.id])] : current.filter((id) => id !== alarm.id))} /></td>
              <td className="p-3"><span className={`rounded border px-2 py-1 text-[10px] font-bold ${LEVEL_STYLES[alarm.level]}`}>{alarm.level}</span></td>
              <td className="p-3"><div className="font-semibold text-gray-800">{alarm.message}</div><div className="mt-1 text-gray-500">{alarm.entity_name || alarm.entity_id || '—'}</div></td>
              <td className="p-3">{alarm.node_name || '—'}</td><td className="p-3">{new Date(alarm.created_at).toLocaleString('zh-CN', { hour12: false })}</td><td className="p-3">{alarm.duration_seconds ?? 0}s</td>
              <td className="p-3">{alarm.state === 'pending' ? '触发待确认' : alarm.state === 'active_unacknowledged' ? '活动未确认' : alarm.state === 'active_acknowledged' ? '活动已确认' : alarm.archived_at ? '已归档' : '已恢复'}</td>
              <td className="p-3" onClick={(event) => event.stopPropagation()}><div className="flex gap-2">{alarm.state === 'active_unacknowledged' && <button type="button" onClick={() => void handleAckIds([alarm.id])} className="neu-btn zizu-primary px-3 py-1.5 font-medium">确认</button>}{canArchive && canArchiveAlarmEvent(alarm) && <button type="button" onClick={() => void handleArchive(alarm)} className="neu-btn px-3 py-1.5 text-gray-600">归档</button>}</div></td>
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

      {(detail || detailLoading) && <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 p-4" role="dialog" aria-modal="true" aria-label="告警详情">
        <div className="neu-card max-h-[90vh] w-full max-w-3xl overflow-y-auto p-5">
          <div className="flex items-start justify-between gap-3"><div><h3 className="text-base font-bold text-gray-800">告警详情</h3><p className="mt-1 font-mono text-[10px] text-gray-400">{detail?.id}</p></div><button type="button" onClick={() => { detailRequest.current?.abort(); detailRequest.current = null; setDetailLoading(false); setDetail(null); setTransitions([]) }} className="neu-btn px-3 py-2 text-xs">关闭</button></div>
          {detailLoading ? <p className="py-10 text-center text-sm text-gray-500">正在读取事件和流转证据…</p> : detail && <div className="mt-4 space-y-4 text-xs">
            <dl className="grid gap-3 sm:grid-cols-2"><div><dt className="text-gray-400">告警 / 等级</dt><dd className="mt-1 font-semibold">{detail.alarm_name} · {detail.severity}</dd></div><div><dt className="text-gray-400">状态</dt><dd className="mt-1">{detail.state}</dd></div><div><dt className="text-gray-400">节点 / 实体</dt><dd className="mt-1">{detail.node_name} / {detail.entity_name}</dd></div><div><dt className="text-gray-400">定义 / 实例</dt><dd className="mt-1 break-all font-mono text-[10px]">{detail.definition_id}<br />{detail.entity_instance_id}</dd></div><div><dt className="text-gray-400">触发 / 恢复</dt><dd className="mt-1">{detail.active_at || detail.pending_at}<br />{detail.recovered_at || '尚未恢复'}</dd></div><div><dt className="text-gray-400">确认记录</dt><dd className="mt-1">{detail.acknowledged_at ? `${detail.acknowledged_by || '未知'} · ${detail.acknowledged_at}${detail.acknowledgement_note ? ` · ${detail.acknowledgement_note}` : ''}` : '尚未确认'}</dd></div></dl>
            <div><h4 className="font-semibold text-gray-700">状态流转与证据</h4><div className="mt-2 space-y-2">{transitions.map((item) => <div key={item.id} className="neu-inset p-3"><div className="flex flex-wrap justify-between gap-2"><span className="font-semibold">{item.code} · {item.from_state || '开始'} → {item.to_state}</span><span>{item.occurred_at}</span></div>{(item.actor || item.note) && <p className="mt-1 text-gray-500">{item.actor || '系统'}{item.note ? ` · ${item.note}` : ''}</p>}{item.evidence && <pre className="mt-2 whitespace-pre-wrap break-all rounded bg-white/50 p-2 font-mono text-[10px]">{JSON.stringify(item.evidence, null, 2)}</pre>}</div>)}{!transitions.length && <p className="text-gray-400">暂无状态流转。</p>}</div></div>
          </div>}
        </div>
      </div>}
    </div>
  )
}

export default function AlarmCenterPage({ actorId, canConfigure }: { actorId: string; canConfigure: boolean }) {
  const [tab, setTab] = useState<'events' | 'notifications' | 'rules'>('events')
  return <div className="tablet-applications space-y-4" data-testid="tablet-alarm-applications">
    <div className="flex gap-2 border-b border-white/70 pb-2">
      <button onClick={() => setTab('events')} className={`rounded-lg border border-transparent px-4 py-2 text-sm font-semibold ${tab === 'events' ? 'zizu-tab-active' : 'text-gray-600'}`}>当前告警</button>
      <button onClick={() => setTab('notifications')} className={`rounded-lg border border-transparent px-4 py-2 text-sm font-semibold ${tab === 'notifications' ? 'zizu-tab-active' : 'text-gray-600'}`}>通知记录</button>
      {canConfigure && <button onClick={() => setTab('rules')} className={`rounded-lg border border-transparent px-4 py-2 text-sm font-semibold ${tab === 'rules' ? 'zizu-tab-active' : 'text-gray-600'}`}>告警规则</button>}
    </div>
    {tab === 'events' && <CurrentAlarmView canArchive={canConfigure} />}
    {tab === 'notifications' && <AlarmNotificationRecords canManage={canConfigure} />}
    {tab === 'rules' && canConfigure && <MinimalAlarmRulesPage key={actorId} />}
  </div>
}
