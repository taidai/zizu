import { useEffect, useState } from 'react'
import { fetchAlarms, acknowledgeAlarm, fetchAlarmEntities, type Alarm, type AlarmLevel } from '../api/client'
import MinimalAlarmRulesPage from './MinimalAlarmRulesPage'
import AlarmNotificationRecords from '../components/alarm-center/AlarmNotificationRecords'

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

function CurrentAlarmView() {
  const [alarms, setAlarms] = useState<Alarm[]>([])
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const [levelFilter, setLevelFilter] = useState<AlarmLevel | ''>('')
  const [entityFilter, setEntityFilter] = useState<string>('')
  const [alarmEntities, setAlarmEntities] = useState<{ id: string; name: string; display_name: string | null }[]>([])
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'acknowledged' | 'resolved'>('active')
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [stats, setStats] = useState<Stats>({ active: 0, unack: 0, critical: 0 })
  const pageSize = 50

  const load = async (targetPage = page) => {
    setLoading(true)
    try {
      const level = levelFilter || undefined
      const entityId = entityFilter || undefined
      const acknowledged = statusFilter === 'acknowledged' ? true : statusFilter === 'active' ? false : undefined
      const resolved = statusFilter === 'resolved' ? true : statusFilter === 'active' ? false : undefined
      const data = await fetchAlarms(targetPage, pageSize, level, undefined, acknowledged, resolved, undefined, entityId)
      setAlarms(data.alarms)
      setTotalPages(data.total_pages || 1)
      setStats({
        active: data.summary.active,
        unack: data.summary.unacknowledged,
        critical: data.summary.critical,
      })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchAlarmEntities().then((d) => setAlarmEntities(d.items)).catch(() => {})
  }, [])

  useEffect(() => {
    setPage(1)
    load(1)
  }, [levelFilter, statusFilter, entityFilter])

  useEffect(() => {
    load(page)
  }, [page])

  useEffect(() => {
    if (!autoRefresh) return
    const id = setInterval(() => load(page), 5000)
    return () => clearInterval(id)
  }, [autoRefresh, page, levelFilter, statusFilter, entityFilter])

  const handleAck = async (alarm: Alarm) => {
    try {
      await acknowledgeAlarm(alarm.id)
      load(page)
    } catch {
      alert('确认失败')
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
        <button onClick={() => setLevelFilter(levelFilter === 'CRITICAL' ? '' : 'CRITICAL')} className={`neu-card p-3 text-left transition ${levelFilter === 'CRITICAL' ? 'ring-2 ring-red-400' : ''}`}>
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
            { key: 'all', label: '全部' },
          ].map((s) => (
            <button
              key={s.key}
              onClick={() => setStatusFilter(s.key as any)}
              className={`neu-btn px-3 py-1 text-xs ${statusFilter === s.key ? 'bg-[#52c41a] text-white' : 'text-gray-600'}`}
            >
              {s.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-gray-500">实体:</span>
          <select
            value={entityFilter}
            onChange={(e) => setEntityFilter(e.target.value)}
            className="neu-input text-xs px-2 py-1 bg-white border border-gray-200 rounded"
          >
            <option value="">全部实体</option>
            {alarmEntities.map((ent) => (
              <option key={ent.id} value={ent.id}>{ent.display_name || ent.name}</option>
            ))}
          </select>
          {entityFilter && (
            <button
              onClick={() => setEntityFilter('')}
              className="text-[10px] text-gray-400 hover:text-gray-600"
            >
              清除
            </button>
          )}
        </div>
      </div>

      {/* 告警列表 */}
      <div className="space-y-2">
        {loading && <div className="text-xs text-gray-400">加载中...</div>}
        {!loading &&
          alarms.map((alarm) => (
            <div
              key={alarm.id}
              className={`neu-card p-3 transition ${
                alarm.acknowledged ? 'opacity-70' : alarm.level === 'CRITICAL' ? 'border-l-4 border-l-red-500' : ''
              }`}
            >
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-center gap-2 mb-1">
                    <span
                      className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                        LEVEL_STYLES[alarm.level] || 'bg-gray-100 text-gray-600 border-gray-200'
                      }`}
                    >
                      {alarm.level}
                    </span>
                    {alarm.entity_name && (
                      <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-indigo-100 text-indigo-700 border border-indigo-200">
                        {alarm.entity_name}
                      </span>
                    )}
                    <span className="text-xs text-gray-400">{new Date(alarm.created_at).toLocaleString('zh-CN', { hour12: false })}</span>
                  </div>
                  <h3 className="text-sm font-bold text-gray-800">{alarm.message}</h3>
                  <p className="text-xs text-gray-500 mt-0.5">
                    {alarm.node_name ? `${alarm.node_name} · ` : ''}持续 {alarm.duration_seconds ?? 0} 秒 · {alarm.state === 'pending' ? '触发待确认' : alarm.resolved_at ? '已恢复' : alarm.acknowledged ? '活动已确认' : '活动未确认'}
                  </p>
                </div>
                <div className="flex flex-col items-end gap-2">
                  {alarm.state === 'active_unacknowledged' && (
                    <button
                      onClick={() => handleAck(alarm)}
                      className="neu-btn px-3 py-1 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d]"
                    >
                      确认
                    </button>
                  )}
                  {alarm.acknowledged && (
                    <span className="text-[10px] text-gray-400">
                      已确认 {alarm.ack_user ? `by ${alarm.ack_user}` : ''}
                    </span>
                  )}
                  {alarm.resolved_at && (
                    <span className="text-[10px] text-green-600">已恢复</span>
                  )}
                </div>
              </div>
            </div>
          ))}
        {alarms.length === 0 && !loading && (
          <div className="neu-card p-8 text-center text-gray-400 text-sm">
            当前筛选条件下无告警
          </div>
        )}
      </div>

      {/* 分页 */}
      {totalPages > 1 && (
        <div className="flex justify-center items-center gap-2 text-xs text-gray-500">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="neu-btn w-7 h-7 flex items-center justify-center disabled:opacity-30"
          >
            ‹
          </button>
          <span className="px-2 font-mono">{page} / {totalPages}</span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
            className="neu-btn w-7 h-7 flex items-center justify-center disabled:opacity-30"
          >
            ›
          </button>
        </div>
      )}
    </div>
  )
}

export default function AlarmCenterPage({ actorId, canConfigure }: { actorId: string; canConfigure: boolean }) {
  const [tab, setTab] = useState<'events' | 'notifications' | 'rules'>('events')
  return <div className="space-y-4">
    <div className="flex gap-2 border-b border-white/70 pb-2">
      <button onClick={() => setTab('events')} className={`rounded-lg px-4 py-2 text-sm font-semibold ${tab === 'events' ? 'bg-[#52c41a] text-white' : 'text-gray-600'}`}>当前告警</button>
      <button onClick={() => setTab('notifications')} className={`rounded-lg px-4 py-2 text-sm font-semibold ${tab === 'notifications' ? 'bg-[#52c41a] text-white' : 'text-gray-600'}`}>通知记录</button>
      {canConfigure && <button onClick={() => setTab('rules')} className={`rounded-lg px-4 py-2 text-sm font-semibold ${tab === 'rules' ? 'bg-[#52c41a] text-white' : 'text-gray-600'}`}>告警规则</button>}
    </div>
    {tab === 'events' && <CurrentAlarmView />}
    {tab === 'notifications' && <AlarmNotificationRecords canRetry={canConfigure} />}
    {tab === 'rules' && canConfigure && <MinimalAlarmRulesPage key={actorId} />}
  </div>
}
