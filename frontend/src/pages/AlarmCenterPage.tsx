import { useEffect, useState } from 'react'
import { fetchAlarms, fetchAlarmGroupCounts, acknowledgeAlarm, resolveAlarm, fetchAlarmLevels, fetchAlarmEntities, type Alarm, type AlarmLevel, type AlarmLevelEntity } from '../api/client'

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

export default function AlarmCenterPage({ canConfigure = true, canResolve = true }: { canConfigure?: boolean; canResolve?: boolean }) {
  const [alarms, setAlarms] = useState<Alarm[]>([])
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const [levelFilter, setLevelFilter] = useState<AlarmLevel | ''>('')
  const [groupFilter, setGroupFilter] = useState<string>('')
  const [groupCounts, setGroupCounts] = useState<Record<string, number>>({})
  const [entityFilter, setEntityFilter] = useState<string>('')
  const [alarmEntities, setAlarmEntities] = useState<{ id: string; name: string; display_name: string | null }[]>([])
  const [alarmLevels, setAlarmLevels] = useState<AlarmLevelEntity[]>([])
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'acknowledged' | 'resolved'>('active')
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [stats, setStats] = useState<Stats>({ total: 0, unack: 0, byLevel: { CRITICAL: 0, MAJOR: 0, WARNING: 0, INFO: 0 } })
  const pageSize = 50

  const load = async (targetPage = page) => {
    setLoading(true)
    try {
      const level = levelFilter || undefined
      const sourceKey = groupFilter || undefined
      const entityId = entityFilter || undefined
      const acknowledged = statusFilter === 'acknowledged' ? true : statusFilter === 'active' ? false : undefined
      const resolved = statusFilter === 'resolved' ? true : statusFilter === 'active' ? false : undefined
      const [data, counts] = await Promise.all([
        fetchAlarms(targetPage, pageSize, level, sourceKey, acknowledged, resolved, undefined, entityId),
        fetchAlarmGroupCounts(),
      ])
      setGroupCounts(counts || {})
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
    if (canConfigure) fetchAlarmLevels(true).then((d) => setAlarmLevels(d.items)).catch(() => {})
    fetchAlarmEntities().then((d) => setAlarmEntities(d.items)).catch(() => {})
  }, [canConfigure])

  useEffect(() => {
    setPage(1)
    load(1)
  }, [levelFilter, statusFilter, groupFilter, entityFilter])

  useEffect(() => {
    load(page)
  }, [page])

  useEffect(() => {
    if (!autoRefresh) return
    const id = setInterval(() => load(page), 5000)
    return () => clearInterval(id)
  }, [autoRefresh, page, levelFilter, statusFilter, groupFilter, entityFilter])

  const handleAck = async (alarm: Alarm) => {
    try {
      await acknowledgeAlarm(alarm.id)
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

  const levelBadgeStyle = (level: AlarmLevelEntity) => {
    return LEVEL_STYLES[level.severity]
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-bold text-gray-800">告警中心</h2>
          <p className="text-xs text-gray-500">查看并处理由规则引擎、MQTT 分级告警与全局实体告警触发的实时告警。</p>
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
          <div className="text-lg font-bold text-gray-800 font-mono-value">{stats.unack}</div>
        </div>
        {(['CRITICAL', 'MAJOR', 'WARNING', 'INFO'] as AlarmLevel[]).map((lv) => (
          <button
            key={lv}
            onClick={() => setLevelFilter(levelFilter === lv ? '' : lv)}
            className={`neu-card p-3 text-left transition ${levelFilter === lv ? 'ring-2 ring-[#52c41a]' : ''}`}
          >
            <div className="text-[10px] text-gray-400 uppercase">{lv}</div>
            <div className={`text-lg font-bold font-mono-value ${
              lv === 'CRITICAL' ? 'text-red-600' : lv === 'MAJOR' ? 'text-orange-600' : lv === 'WARNING' ? 'text-amber-600' : 'text-blue-600'
            }`}>
              {stats.byLevel[lv]}
            </div>
          </button>
        ))}
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
        {canConfigure && <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-gray-500">告警等级:</span>
          {alarmLevels.map((g) => (
            <button
              key={g.code}
              onClick={() => setGroupFilter(groupFilter === g.code ? '' : g.code)}
              className={`neu-btn px-3 py-1.5 text-xs font-medium ${
                groupFilter === g.code ? levelBadgeStyle(g) : 'text-gray-600'
              }`}
            >
              {g.name}
              <span className="ml-1.5 font-mono-value">{groupCounts[g.code] || 0}</span>
            </button>
          ))}
        </div>}
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
                    {alarm.source_key && (
                      <span
                        className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                          LEVEL_STYLES[alarm.level] || 'bg-gray-100 text-gray-600 border-gray-200'
                        }`}
                      >
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
                    {alarm.entity_name && (
                      <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-indigo-100 text-indigo-700 border border-indigo-200">
                        {alarm.entity_name}
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
                  {canResolve && alarm.acknowledged && !alarm.resolved_at && (
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
