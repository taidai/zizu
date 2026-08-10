import { useEffect, useState } from 'react'
import { fetchAlarms, fetchAlarmGroupCounts, acknowledgeAlarm, resolveAlarm, type Alarm, type AlarmLevel } from '../api/client'

const LEVELS: AlarmLevel[] = ['CRITICAL', 'MAJOR', 'WARNING', 'INFO']

const SOURCE_KEY_STYLES: Record<string, string> = {
  error1: 'bg-red-100 text-red-700 border-red-200',
  error2: 'bg-orange-100 text-orange-700 border-orange-200',
  error3: 'bg-amber-100 text-amber-700 border-amber-200',
}

const LEVEL_STYLES: Record<AlarmLevel, string> = {
  CRITICAL: 'bg-red-100 text-red-700 border-red-200',
  MAJOR: 'bg-orange-100 text-orange-700 border-orange-200',
  WARNING: 'bg-amber-100 text-amber-700 border-amber-200',
  INFO: 'bg-blue-100 text-blue-700 border-blue-200',
}

interface Stats {
  total: number
  unack: number
  byLevel: Record<AlarmLevel, number>
}

const ERROR_GROUPS: Array<{ key: 'error1' | 'error2' | 'error3'; label: string }> = [
  { key: 'error1', label: 'error1' },
  { key: 'error2', label: 'error2' },
  { key: 'error3', label: 'error3' },
]

export default function AlarmCenterPage() {
  const [alarms, setAlarms] = useState<Alarm[]>([])
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const [levelFilter, setLevelFilter] = useState<AlarmLevel | ''>('')
  const [groupFilter, setGroupFilter] = useState<'error1' | 'error2' | 'error3' | ''>('')
  const [groupCounts, setGroupCounts] = useState<Record<string, number>>({ error1: 0, error2: 0, error3: 0 })
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'acknowledged' | 'resolved'>('active')
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [stats, setStats] = useState<Stats>({ total: 0, unack: 0, byLevel: { CRITICAL: 0, MAJOR: 0, WARNING: 0, INFO: 0 } })
  const pageSize = 50

  const load = async (targetPage = page) => {
    setLoading(true)
    try {
      const level = levelFilter || undefined
      const sourceKey = groupFilter || undefined
      const acknowledged = statusFilter === 'acknowledged' ? true : statusFilter === 'active' ? false : undefined
      const resolved = statusFilter === 'resolved' ? true : statusFilter === 'active' ? false : undefined
      const [data, counts] = await Promise.all([
        fetchAlarms(targetPage, pageSize, level, sourceKey, acknowledged, resolved),
        fetchAlarmGroupCounts(),
      ])
      setGroupCounts(counts)
      setAlarms(data.alarms)
      setTotalPages(data.total_pages || 1)
      setStats({
        total: data.total,
        unack: data.alarms.filter((a) => !a.acknowledged && !a.resolved_at).length,
        byLevel: {
          CRITICAL: data.alarms.filter((a) => a.level === 'CRITICAL').length,
          MAJOR: data.alarms.filter((a) => a.level === 'MAJOR').length,
          WARNING: data.alarms.filter((a) => a.level === 'WARNING').length,
          INFO: data.alarms.filter((a) => a.level === 'INFO').length,
        },
      })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setPage(1)
    load(1)
  }, [levelFilter, statusFilter, groupFilter])

  useEffect(() => {
    load(page)
  }, [page])

  useEffect(() => {
    if (!autoRefresh) return
    const id = setInterval(() => load(page), 5000)
    return () => clearInterval(id)
  }, [autoRefresh, page, levelFilter, statusFilter, groupFilter])

  const handleAck = async (alarm: Alarm) => {
    try {
      await acknowledgeAlarm(alarm.id, 'operator')
      load(page)
    } catch {
      alert('确认失败')
    }
  }

  const handleResolve = async (alarm: Alarm) => {
    try {
      await resolveAlarm(alarm.id)
      load(page)
    } catch {
      alert('恢复失败')
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-bold text-gray-800">告警中心</h2>
          <p className="text-xs text-gray-500">查看并处理由规则引擎与 MQTT 分级告警触发的实时告警。</p>
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
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <div className="neu-card p-3">
          <div className="text-[10px] text-gray-400 uppercase">全部告警</div>
          <div className="text-lg font-bold text-gray-800 font-mono-value">{stats.total}</div>
        </div>
        <div className="neu-card p-3">
          <div className="text-[10px] text-gray-400 uppercase">未确认</div>
          <div className="text-lg font-bold text-red-600 font-mono-value">{stats.unack}</div>
        </div>
        {LEVELS.map((level) => (
          <div key={level} className="neu-card p-3">
            <div className="text-[10px] text-gray-400 uppercase">{level}</div>
            <div className="text-lg font-bold text-gray-800 font-mono-value">{stats.byLevel[level]}</div>
          </div>
        ))}
      </div>

      {/* 分组统计卡 */}
      <div className="grid grid-cols-3 gap-3">
        {ERROR_GROUPS.map((g) => (
          <button
            key={g.key}
            onClick={() => setGroupFilter(groupFilter === g.key ? '' : g.key)}
            className={`neu-card p-3 text-left border-2 transition-all ${
              groupFilter === g.key ? 'border-[#52c41a]' : 'border-transparent'
            }`}
          >
            <div className="flex items-center justify-between">
              <span className={`text-xs font-bold px-2 py-0.5 rounded border ${SOURCE_KEY_STYLES[g.key]}`}>{g.label}</span>
              <span className="text-lg font-bold text-gray-800 font-mono-value">{groupCounts[g.key] || 0}</span>
            </div>
            <div className="text-[10px] text-gray-400 mt-1">未恢复告警数</div>
          </button>
        ))}
      </div>

      {/* 筛选 */}
      <div className="neu-card p-3 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">严重度:</span>
          <select
            value={levelFilter}
            onChange={(e) => setLevelFilter(e.target.value as AlarmLevel | '')}
            className="neu-input px-3 py-1.5 text-xs bg-transparent"
          >
            <option value="">全部</option>
            {LEVELS.map((l) => (
              <option key={l} value={l}>{l}</option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">状态:</span>
          {[
            { key: 'active', label: '活动中' },
            { key: 'acknowledged', label: '已确认' },
            { key: 'resolved', label: '已恢复' },
            { key: 'all', label: '全部' },
          ].map((s) => (
            <button
              key={s.key}
              onClick={() => setStatusFilter(s.key as any)}
              className={`px-3 py-1 text-xs rounded-full font-medium transition-colors ${
                statusFilter === s.key ? 'bg-[#52c41a] text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
        <button
          onClick={() => load(page)}
          disabled={loading}
          className="neu-btn px-4 py-1.5 text-xs font-medium text-[#389e0d] disabled:opacity-50 ml-auto"
        >
          {loading ? '刷新中...' : '立即刷新'}
        </button>
      </div>

      {/* 告警列表 — 按 error1/2/3 分组 */}
      <div className="space-y-4">
        {(['error1', 'error2', 'error3'] as const)
          .filter((g) => !groupFilter || groupFilter === g)
          .map((g) => {
            const groupAlarms = alarms.filter((a) => a.source_key === g)
            if (groupFilter !== g && groupAlarms.length === 0) return null
            return (
              <div key={g} className="space-y-3">
                <div className={`flex items-center justify-between px-3 py-2 rounded border ${SOURCE_KEY_STYLES[g] || 'bg-gray-100 text-gray-600 border-gray-200'}`}>
                  <div className="flex items-center gap-2 font-bold text-xs">
                    <span>{g.toUpperCase()}</span>
                    <span className="font-mono opacity-80">{groupAlarms.length}</span>
                  </div>
                  <button
                    onClick={() => setGroupFilter(groupFilter === g ? '' : g)}
                    className="text-[10px] underline opacity-80 hover:opacity-100"
                  >
                    {groupFilter === g ? '显示全部' : '仅看该组'}
                  </button>
                </div>
                {groupAlarms.map((alarm) => (
                  <div
                    key={alarm.id}
                    className={`neu-card p-4 border ${
                      alarm.acknowledged ? 'border-dashed border-gray-200 opacity-80' : LEVEL_STYLES[alarm.level]
                    }`}
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex-1">
                        <div className="flex items-center gap-2 mb-1">
                          <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${LEVEL_STYLES[alarm.level]}`}>
                            {alarm.level}
                          </span>
                          {alarm.source_key && (
                            <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${SOURCE_KEY_STYLES[alarm.source_key] || 'bg-gray-100 text-gray-600 border-gray-200'}`}>
                              {alarm.source_key}
                            </span>
                          )}
                          {alarm.alarm_type && (
                            <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-purple-100 text-purple-700 border border-purple-200">
                              {alarm.alarm_type}
                            </span>
                          )}
                          {alarm.alarm_source && (
                            <span className="px-2 py-0.5 rounded text-[10px] text-gray-500 bg-gray-50 border border-gray-200">
                              {alarm.alarm_source}
                            </span>
                          )}
                          {alarm.alarm_count && alarm.alarm_count > 1 && (
                            <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-orange-100 text-orange-700 border border-orange-200">
                              ×{alarm.alarm_count}
                            </span>
                          )}
                          <span className="text-xs text-gray-400">{new Date(alarm.created_at).toLocaleString('zh-CN', { hour12: false })}</span>
                          {alarm.rule_name && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#534AB7]/10 text-[#534AB8]">
                              {alarm.rule_name}
                            </span>
                          )}
                        </div>
                        <h3 className="text-sm font-bold text-gray-800">{alarm.message}</h3>
                        <p className="text-xs text-gray-500 mt-0.5">
                          来源: {alarm.external_id || alarm.node_name || alarm.node_id || 'MQTT告警'}
                          {alarm.alarm_threshold != null && <span className="ml-2 text-[10px] text-amber-600">阈值:{alarm.alarm_threshold}</span>}
                          {alarm.alarm_code && <span className="ml-2 text-[10px] text-gray-400 font-mono">编码:{alarm.alarm_code}</span>}
                          {alarm.source_topic && <span className="ml-2 text-[10px] text-gray-400 font-mono">{alarm.source_topic}</span>}
                        </p>
                      </div>
                      <div className="flex flex-col items-end gap-2">
                        {!alarm.acknowledged && !alarm.resolved_at && (
                          <button
                            onClick={() => handleAck(alarm)}
                            className="neu-btn px-3 py-1 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d]"
                          >
                            确认
                          </button>
                        )}
                        {alarm.acknowledged && !alarm.resolved_at && (
                          <button
                            onClick={() => handleResolve(alarm)}
                            className="neu-btn px-3 py-1 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600"
                          >
                            恢复
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
              </div>
            )
          })}
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
