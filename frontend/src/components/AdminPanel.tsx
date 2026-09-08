import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import {
  fetchPipelineConfig, updatePipelineConfig, executeSql, truncateTable,
  fetchMqttConfig, updateMqttConfig, fetchHealth,
  type PipelineConfig, type SqlQueryResult, type MqttConfig, type HealthStatus,
} from '../api/client'
import DataBrowser from './DataBrowser'
import NanoMQManager from './NanoMQManager'
import FaultMapManager from './FaultMapManager'
import AlarmHttpNotificationPanel from './admin/AlarmHttpNotificationPanel'
import { restoreModalTrigger, useModalFocus } from './useModalFocus'
import './alarm-center/tabletApplications.css'
import './admin/systemTools.css'

type ToolKey = 'messaging' | 'notifications' | 'faults' | 'data'

const TOOL_GROUPS: Array<{ key: ToolKey; title: string; eyebrow: string; description: string; actions: string[] }> = [
  {
    key: 'messaging',
    title: 'NanoMQ / MQTT',
    eyebrow: '消息接入',
    description: '查看采集节拍与代理状态，维护北向主题、ACL 和 NanoMQ 配置。',
    actions: ['Pipeline', '主题', '客户端', 'ACL'],
  },
  {
    key: 'notifications',
    title: 'HTTP 通知',
    eyebrow: '出站集成',
    description: '维护告警 HTTP 请求，使用脱敏预览，并查看每次真实测试收据。',
    actions: ['CRUD', '测试', '启停', '脱敏'],
  },
  {
    key: 'faults',
    title: '故障映射',
    eyebrow: '配置资产',
    description: '维护故障码与人类可读说明，供正式点位和告警配置复用。',
    actions: ['映射表', '条目', '引用保护'],
  },
  {
    key: 'data',
    title: '数据与系统状态',
    eyebrow: '诊断维护',
    description: '浏览真实遥测、执行只读 SQL；永久清理保持显著范围与确认。',
    actions: ['数据浏览', '只读 SQL', '危险清理'],
  },
]

function apiMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : '未知错误'
}

function ToolDialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const modal = useModalFocus({ open: true, onClose })
  return (
    <div className="zizu-tools-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
      <section ref={modal.dialogRef} tabIndex={-1} onKeyDown={modal.onKeyDown} role="dialog" aria-modal="true" aria-label={title} className="zizu-tools-dialog">
        <header className="zizu-tools-dialog-header">
          <div>
            <span>系统工具</span>
            <h2>{title}</h2>
          </div>
          <button type="button" onClick={onClose} className="neu-btn zizu-tools-close">关闭</button>
        </header>
        <div className="zizu-tools-dialog-body">{children}</div>
      </section>
    </div>
  )
}

export default function AdminPanel() {
  const [activeTool, setActiveTool] = useState<ToolKey | null>(null)
  const [truncateOpen, setTruncateOpen] = useState(false)
  const toolTriggers = useRef<Partial<Record<ToolKey, HTMLButtonElement>>>({})
  const truncateTrigger = useRef<HTMLButtonElement | null>(null)
  const systemHealthGeneration = useRef(0)
  // 入库节拍
  const [config, setConfig] = useState<PipelineConfig | null>(null)
  const [configLoading, setConfigLoading] = useState(true)
  const [configError, setConfigError] = useState('')
  const [configSaving, setConfigSaving] = useState(false)
  const [configMsg, setConfigMsg] = useState('')

  // MQTT 主题
  const [mqttConfig, setMqttConfig] = useState<MqttConfig | null>(null)
  const [mqttLoading, setMqttLoading] = useState(true)
  const [mqttError, setMqttError] = useState('')
  const [mqttSaving, setMqttSaving] = useState(false)
  const [mqttMsg, setMqttMsg] = useState('')

  // 系统健康状态
  const [systemHealth, setSystemHealth] = useState<HealthStatus | null>(null)
  const [systemHealthLoading, setSystemHealthLoading] = useState(false)
  const [systemHealthError, setSystemHealthError] = useState('')

  // SQL 查询
  const [sql, setSql] = useState('SELECT * FROM t_telemetry ORDER BY ts DESC LIMIT 100')
  const [sqlResult, setSqlResult] = useState<SqlQueryResult | null>(null)
  const [sqlLoading, setSqlLoading] = useState(false)
  const [sqlError, setSqlError] = useState('')

  // 清空表
  const [truncateTableName, setTruncateTableName] = useState('t_telemetry')
  const [truncateConfirm, setTruncateConfirm] = useState('')
  const [truncateLoading, setTruncateLoading] = useState(false)
  const [truncateMsg, setTruncateMsg] = useState('')

  const closeTruncate = () => {
    setTruncateOpen(false)
    restoreModalTrigger(truncateTrigger.current)
  }
  const truncateModal = useModalFocus({ open: truncateOpen, onClose: closeTruncate })

  const loadPipelineConfig = useCallback(async () => {
    setConfigLoading(true)
    setConfigError('')
    setConfig(null)
    try {
      setConfig(await fetchPipelineConfig())
    } catch (reason) {
      setConfigError(`Pipeline 配置读取失败：${apiMessage(reason)}`)
    } finally {
      setConfigLoading(false)
    }
  }, [])

  const loadMqttConfig = useCallback(async () => {
    setMqttLoading(true)
    setMqttError('')
    setMqttConfig(null)
    try {
      setMqttConfig(await fetchMqttConfig())
    } catch (reason) {
      setMqttError(`MQTT 配置读取失败：${apiMessage(reason)}`)
    } finally {
      setMqttLoading(false)
    }
  }, [])

  const loadSystemHealth = useCallback(async () => {
    const generation = ++systemHealthGeneration.current
    setSystemHealthLoading(true)
    setSystemHealthError('')
    try {
      const nextHealth = await fetchHealth()
      if (generation === systemHealthGeneration.current) setSystemHealth(nextHealth)
    } catch (reason) {
      if (generation === systemHealthGeneration.current) {
        setSystemHealth(null)
        setSystemHealthError(`系统状态读取失败：${apiMessage(reason)}。连接中断，当前状态未知。`)
      }
    } finally {
      if (generation === systemHealthGeneration.current) setSystemHealthLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadPipelineConfig()
    void loadMqttConfig()
  }, [loadMqttConfig, loadPipelineConfig])

  useEffect(() => {
    if (activeTool !== 'data') return
    let closed = false
    let nextPoll: number | undefined
    const poll = async () => {
      await loadSystemHealth()
      if (!closed) nextPoll = window.setTimeout(() => { void poll() }, 5000)
    }
    void poll()
    return () => {
      closed = true
      if (nextPoll !== undefined) window.clearTimeout(nextPoll)
      systemHealthGeneration.current += 1
    }
  }, [activeTool, loadSystemHealth])

  const closeTool = () => {
    const key = activeTool
    setActiveTool(null)
    setTruncateOpen(false)
    if (key) restoreModalTrigger(toolTriggers.current[key] || null)
  }

  const handleSaveConfig = async () => {
    if (!config) return
    setConfigSaving(true)
    setConfigMsg('')
    try {
      await updatePipelineConfig(config)
      setConfigMsg('配置已保存并生效')
    } catch (reason) {
      setConfigMsg(`保存失败：${apiMessage(reason)}`)
    } finally {
      setConfigSaving(false)
    }
  }

  const handleSaveMqtt = async () => {
    if (!mqttConfig) return
    setMqttSaving(true)
    setMqttMsg('')
    try {
      const result = await updateMqttConfig({ mqtt_telemetry_topic: mqttConfig.mqtt_telemetry_topic })
      setMqttConfig(result)
      setMqttMsg('MQTT 主题已保存并实时重订阅')
    } catch (reason) {
      setMqttMsg(`保存失败：${apiMessage(reason)}`)
    } finally {
      setMqttSaving(false)
    }
  }

  const handleExecuteSql = async () => {
    setSqlLoading(true)
    setSqlError('')
    setSqlResult(null)
    try {
      const result = await executeSql(sql, 500)
      setSqlResult(result)
    } catch (e: any) {
      setSqlError(e.message || '查询失败')
    } finally {
      setSqlLoading(false)
    }
  }

  const handleTruncate = async () => {
    if (truncateConfirm.toLowerCase() !== 'yes') {
      setTruncateMsg('请输入 yes 确认')
      return
    }
    setTruncateLoading(true)
    setTruncateMsg('')
    try {
      const result = await truncateTable(truncateTableName, truncateConfirm)
      setTruncateMsg(`已清空 ${result.table}，删除 ${result.rows_deleted} 行`)
      setTruncateConfirm('')
      closeTruncate()
    } catch (e: any) {
      setTruncateMsg(e.message || '操作失败')
    } finally {
      setTruncateLoading(false)
    }
  }

  const messagingTools = (
    <div className="zizu-tools-stack">
      <section className="neu-card p-4" aria-label="Pipeline 配置">
        <h3 className="text-sm font-bold text-gray-800 mb-3">入库节拍配置</h3>
        {configLoading && <p role="status" className="text-xs text-gray-500">正在读取 Pipeline 配置...</p>}
        {configError && <div className="flex flex-wrap items-center gap-3"><p role="alert" className="text-xs text-red-700">{configError}</p><button type="button" onClick={() => void loadPipelineConfig()} className="neu-btn px-3 text-xs">重试 Pipeline</button></div>}
        {config && !configLoading && !configError && <><div className="flex flex-wrap items-center gap-4">
          <label className="text-xs text-gray-600">批量大小
            <input type="number" min={1} max={1000} value={config.batch_size} onChange={(event) => setConfig({ ...config, batch_size: parseInt(event.target.value) || 50 })} className="neu-input ml-2 px-2 py-1 text-xs w-20" />
          </label>
          <label className="text-xs text-gray-600">Flush 间隔
            <input type="number" min={0.1} max={60} step={0.1} value={config.flush_interval_sec} onChange={(event) => setConfig({ ...config, flush_interval_sec: parseFloat(event.target.value) || 1 })} className="neu-input ml-2 px-2 py-1 text-xs w-20" /> 秒
          </label>
          <button type="button" onClick={() => void handleSaveConfig()} disabled={configSaving} className="neu-btn zizu-primary px-4 text-xs font-medium disabled:opacity-50">{configSaving ? '保存中...' : '保存配置'}</button>
          {configMsg && <span role={configMsg.startsWith('保存失败') ? 'alert' : 'status'} className="text-xs text-gray-600">{configMsg}</span>}
        </div>
        <p className="mt-2 text-[11px] text-gray-500">批量达到阈值或定时到期时写入数据库；保存后由正式 Pipeline 运行时生效。</p></>}
      </section>

      <section className="neu-card p-4" aria-label="MQTT 北向主题">
        <h3 className="text-sm font-bold text-gray-800 mb-3">MQTT 北向主题</h3>
        {mqttLoading && <p role="status" className="text-xs text-gray-500">正在读取 MQTT 配置...</p>}
        {mqttError && <div className="flex flex-wrap items-center gap-3"><p role="alert" className="text-xs text-red-700">{mqttError}</p><button type="button" onClick={() => void loadMqttConfig()} className="neu-btn px-3 text-xs">重试 MQTT</button></div>}
        {mqttConfig && !mqttLoading && !mqttError && <><div className="flex flex-wrap items-center gap-3">
          <label className="min-w-[260px] flex-1 text-xs text-gray-600">订阅主题
            <input type="text" value={mqttConfig.mqtt_telemetry_topic} onChange={(event) => setMqttConfig({ ...mqttConfig, mqtt_telemetry_topic: event.target.value })} className="neu-input mt-1 w-full px-3 py-2 text-xs" placeholder="例如 /neuron/#" />
          </label>
          <button type="button" onClick={() => void handleSaveMqtt()} disabled={mqttSaving} className="neu-btn zizu-primary px-4 text-xs font-medium disabled:opacity-50">{mqttSaving ? '保存中...' : '保存并重订阅'}</button>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
          <span>当前生效主题：</span>
          {mqttConfig.effective_topics.length ? mqttConfig.effective_topics.map((topic) => <code key={topic} className="rounded bg-blue-50 px-2 py-1 text-blue-700">{topic}</code>) : <span>无</span>}
        </div>
        {mqttConfig.persisted && mqttConfig.persisted !== mqttConfig.mqtt_telemetry_topic && <p className="mt-2 text-[11px] text-gray-500">数据库持久化值：<code>{mqttConfig.persisted}</code>（保存后覆盖）</p>}
        {mqttMsg && <p role={mqttMsg.startsWith('保存失败') ? 'alert' : 'status'} className="mt-2 text-xs text-gray-600">{mqttMsg}</p>}</>}
      </section>
      <NanoMQManager />
    </div>
  )

  const dataTools = (
    <div className="zizu-tools-stack">
      <section className="neu-card p-4" aria-label="系统健康状态">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><h3 className="text-sm font-bold text-gray-800">系统健康状态</h3><p className="mt-1 text-[11px] text-gray-500">每 5 秒读取正式 health API；未取得响应时不沿用旧状态。</p></div>
          <button type="button" onClick={() => void loadSystemHealth()} disabled={systemHealthLoading} className="neu-btn px-3 text-xs disabled:opacity-50">重试系统状态</button>
        </div>
        {systemHealthLoading && <p role="status" className="mt-3 text-xs text-gray-500">正在读取系统状态...</p>}
        {systemHealthError && <p role="alert" className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{systemHealthError}</p>}
        {systemHealth && !systemHealthLoading && !systemHealthError && (() => {
          const connected = systemHealth.status.toLowerCase() === 'healthy'
            && systemHealth.pipeline.status.toLowerCase() === 'running'
            && Object.values(systemHealth.components).every((component) => component.status.toLowerCase() === 'connected')
          return <div className="mt-3 space-y-3">
            <div className="flex flex-wrap items-center gap-3"><strong className={connected ? 'text-green-700' : 'text-red-700'}>{connected ? `系统健康 ${systemHealth.status}` : '连接异常'}</strong><span className="text-[11px] text-gray-500">API {systemHealth.version} · 运行 {systemHealth.uptime_seconds} 秒</span></div>
            <div className="grid gap-2 sm:grid-cols-3">
              <div className="neu-inset px-3 py-2 text-xs">TimescaleDB <strong>{systemHealth.components.timescaledb.status}</strong></div>
              <div className="neu-inset px-3 py-2 text-xs">MQTT <strong>{systemHealth.components.mqtt.status}</strong></div>
              <div className="neu-inset px-3 py-2 text-xs">Neuron <strong>{systemHealth.components.neuron.status}</strong></div>
            </div>
            <p className="text-xs text-gray-600">Pipeline {systemHealth.pipeline.status} · 消息 {systemHealth.pipeline.messages_received.toLocaleString()} · 入库 {systemHealth.pipeline.points_written_db.toLocaleString()} · 最后消息 {systemHealth.pipeline.last_message_at ? new Date(systemHealth.pipeline.last_message_at).toLocaleString('zh-CN', { hour12: false }) : '无'}</p>
          </div>
        })()}
      </section>
      <DataBrowser />
      <section className="neu-card p-4" aria-label="只读 SQL 查询">
        <div className="flex items-center justify-between gap-3">
          <div><h3 className="text-sm font-bold text-gray-800">只读 SQL 查询</h3><p className="mt-1 text-[11px] text-gray-500">服务端仅接受 SELECT，并限制最多返回 500 行。</p></div>
          <button type="button" onClick={() => void handleExecuteSql()} disabled={sqlLoading} className="neu-btn zizu-primary px-4 text-xs font-medium disabled:opacity-50">{sqlLoading ? '执行中...' : '执行'}</button>
        </div>
        <textarea aria-label="SQL 查询" value={sql} onChange={(event) => setSql(event.target.value)} rows={3} className="neu-input mt-3 w-full px-3 py-2 text-xs font-mono" />
        {sqlError && <p role="alert" className="mt-2 text-xs text-red-600">{sqlError}</p>}
        {sqlResult && <div className="mt-3"><p className="mb-2 text-xs text-gray-500">返回 {sqlResult.row_count} 行</p><div className="max-h-[320px] overflow-auto"><table className="w-full text-xs"><thead><tr>{sqlResult.columns.map((column) => <th key={column} className="px-2 py-1 text-left text-[11px] text-gray-500">{column}</th>)}</tr></thead><tbody>{sqlResult.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex} className="border-t border-gray-100 px-2 py-1 font-mono text-[11px]">{cell === null ? 'NULL' : String(cell)}</td>)}</tr>)}</tbody></table></div></div>}
      </section>

      <section className="neu-card zizu-tools-danger-card p-4" aria-label="危险数据操作">
        <div>
          <h3 className="text-sm font-bold text-red-700">危险数据操作</h3>
          <p className="mt-1 text-xs text-gray-600">永久删除选定表内的全部记录。动作继续由服务端白名单、权限和审计保护。</p>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <select aria-label="要清空的表" value={truncateTableName} onChange={(event) => setTruncateTableName(event.target.value)} className="neu-input px-3 py-2 text-xs bg-transparent">
            <option value="t_telemetry">t_telemetry（遥测数据）</option>
            <option value="t_audit_log">t_audit_log（审计日志）</option>
          </select>
          <button ref={truncateTrigger} type="button" onClick={() => { setTruncateConfirm(''); setTruncateMsg(''); setTruncateOpen(true) }} className="neu-btn zizu-tools-danger-button px-4 text-xs font-semibold">准备清空表</button>
        </div>
        {truncateMsg && <p role={truncateMsg.includes('已清空') ? 'status' : 'alert'} className={`mt-2 text-xs ${truncateMsg.includes('已清空') ? 'text-green-700' : 'text-red-700'}`}>{truncateMsg}</p>}
      </section>
    </div>
  )

  return (
    <div className="tablet-applications zizu-tools" data-testid="tablet-admin-applications">
      <header className="zizu-tools-intro">
        <div><span>PLATFORM OPERATIONS</span><h1>系统工具</h1></div>
        <p>正式维护能力按职责分区。打开分区只读取当前状态，写操作仍逐项调用既有受权 API。</p>
      </header>
      <div className="zizu-tools-grid">
        {TOOL_GROUPS.map((group, index) => (
          <article key={group.key} className="neu-card zizu-tools-card">
            <div className="zizu-tools-card-index">0{index + 1}</div>
            <p>{group.eyebrow}</p>
            <h2>{group.title}</h2>
            <div className="zizu-tools-rule" />
            <p className="zizu-tools-description">{group.description}</p>
            <ul>{group.actions.map((action) => <li key={action}>{action}</li>)}</ul>
            <button ref={(node) => { if (node) toolTriggers.current[group.key] = node }} type="button" onClick={() => setActiveTool(group.key)} className="neu-btn zizu-primary zizu-tools-open">打开{group.title}</button>
          </article>
        ))}
      </div>

      {activeTool && <ToolDialog title={TOOL_GROUPS.find((group) => group.key === activeTool)!.title} onClose={closeTool}>
        {activeTool === 'messaging' && messagingTools}
        {activeTool === 'notifications' && <AlarmHttpNotificationPanel />}
        {activeTool === 'faults' && <FaultMapManager />}
        {activeTool === 'data' && dataTools}
      </ToolDialog>}

      {truncateOpen && (
        <div className="zizu-tools-overlay zizu-tools-danger-overlay">
          <section ref={truncateModal.dialogRef} tabIndex={-1} onKeyDown={truncateModal.onKeyDown} role="dialog" aria-modal="true" aria-label="确认永久清空数据" className="zizu-tools-confirm">
            <span className="zizu-tools-danger-kicker">DANGER / 永久操作</span>
            <h2>确认永久清空数据</h2>
            <p>范围：表 <code>{truncateTableName}</code> 内的全部记录。</p>
            <p className="zizu-tools-confirm-warning">此操作不可恢复。服务端会再次检查白名单、管理员权限并记录审计。</p>
            <label>输入 yes 确认
              <input autoFocus aria-label="输入 yes 确认" value={truncateConfirm} onChange={(event) => setTruncateConfirm(event.target.value)} className="neu-input mt-2 w-full px-3 py-2 text-sm" />
            </label>
            <div className="zizu-tools-confirm-actions">
              <button type="button" onClick={closeTruncate} className="neu-btn px-4 text-xs">取消</button>
              <button type="button" onClick={() => void handleTruncate()} disabled={truncateLoading || truncateConfirm.toLowerCase() !== 'yes'} className="neu-btn zizu-tools-danger-button px-4 text-xs font-semibold disabled:opacity-40">{truncateLoading ? '执行中...' : '永久清空'}</button>
            </div>
          </section>
        </div>
      )}
    </div>
  )
}
