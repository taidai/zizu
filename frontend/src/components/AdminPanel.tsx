import { useEffect, useState } from 'react'
import {
  fetchPipelineConfig, updatePipelineConfig, executeSql, truncateTable,
  fetchMqttConfig, updateMqttConfig,
  type PipelineConfig, type SqlQueryResult, type MqttConfig,
} from '../api/client'
import DataBrowser from './DataBrowser'
import NanoMQManager from './NanoMQManager'
import FaultMapManager from './FaultMapManager'
import AlarmHttpNotificationPanel from './admin/AlarmHttpNotificationPanel'

export default function AdminPanel() {
  // 入库节拍
  const [config, setConfig] = useState<PipelineConfig>({ batch_size: 50, flush_interval_sec: 1.0 })
  const [configSaving, setConfigSaving] = useState(false)
  const [configMsg, setConfigMsg] = useState('')

  // MQTT 主题
  const [mqttConfig, setMqttConfig] = useState<MqttConfig>({ mqtt_telemetry_topic: '/neuron/#', persisted: null, effective_topics: [] })
  const [mqttSaving, setMqttSaving] = useState(false)
  const [mqttMsg, setMqttMsg] = useState('')

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

  useEffect(() => {
    fetchPipelineConfig().then(setConfig).catch(() => {})
    fetchMqttConfig().then(setMqttConfig).catch(() => {})
  }, [])

  const handleSaveConfig = async () => {
    setConfigSaving(true)
    setConfigMsg('')
    try {
      await updatePipelineConfig(config)
      setConfigMsg('配置已保存并生效')
    } catch {
      setConfigMsg('保存失败')
    } finally {
      setConfigSaving(false)
    }
  }

  const handleSaveMqtt = async () => {
    setMqttSaving(true)
    setMqttMsg('')
    try {
      const result = await updateMqttConfig({ mqtt_telemetry_topic: mqttConfig.mqtt_telemetry_topic })
      setMqttConfig(result)
      setMqttMsg('MQTT 主题已保存并实时重订阅')
    } catch {
      setMqttMsg('保存失败')
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
    } catch (e: any) {
      setTruncateMsg(e.message || '操作失败')
    } finally {
      setTruncateLoading(false)
    }
  }

  return (
    <div className="space-y-4">
      {/* 入库节拍配置 */}
      <div className="neu-card p-4">
        <h3 className="text-sm font-bold text-gray-800 mb-3">入库节拍配置</h3>
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-600">批量大小:</label>
            <input
              type="number"
              min={1}
              max={1000}
              value={config.batch_size}
              onChange={(e) => setConfig({ ...config, batch_size: parseInt(e.target.value) || 50 })}
              className="neu-input px-2 py-1 text-xs w-20"
            />
            <span className="text-xs text-gray-400">条</span>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-600">Flush 间隔:</label>
            <input
              type="number"
              min={0.1}
              max={60}
              step={0.1}
              value={config.flush_interval_sec}
              onChange={(e) => setConfig({ ...config, flush_interval_sec: parseFloat(e.target.value) || 1.0 })}
              className="neu-input px-2 py-1 text-xs w-20"
            />
            <span className="text-xs text-gray-400">秒</span>
          </div>
          <button
            onClick={handleSaveConfig}
            disabled={configSaving}
            className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50"
          >
            {configSaving ? '保存中...' : '保存配置'}
          </button>
          {configMsg && <span className="text-xs text-[#389e0d]">{configMsg}</span>}
        </div>
       <p className="text-[11px] text-gray-400 mt-2">
         批量大小：缓冲区达到该条数时立即写入 DB · Flush 间隔：定时强制写入的时间间隔
       </p>
     </div>

      {/* MQTT 北向主题配置 */}
      <div className="neu-card p-4">
        <h3 className="text-sm font-bold text-gray-800 mb-3">MQTT 北向主题配置</h3>
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2 flex-1 min-w-[240px]">
            <label className="text-xs text-gray-600 whitespace-nowrap">订阅主题:</label>
            <input
              type="text"
              value={mqttConfig.mqtt_telemetry_topic}
              onChange={(e) => setMqttConfig({ ...mqttConfig, mqtt_telemetry_topic: e.target.value })}
              placeholder="例如 /neuron/# 或 telemetry/#,/neuron/#"
              className="neu-input px-2 py-1 text-xs flex-1"
            />
          </div>
          <button
            onClick={handleSaveMqtt}
            disabled={mqttSaving}
            className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50"
          >
            {mqttSaving ? '保存中...' : '保存并重订阅'}
          </button>
          {mqttMsg && <span className="text-xs text-[#389e0d]">{mqttMsg}</span>}
        </div>
        <div className="mt-3 flex flex-wrap gap-2 items-center">
          <span className="text-[11px] text-gray-500">当前生效主题:</span>
          {mqttConfig.effective_topics.length === 0 ? (
            <span className="text-[11px] text-gray-400">无</span>
          ) : (
            mqttConfig.effective_topics.map((t) => (
              <span key={t} className="px-2 py-0.5 rounded text-[11px] bg-blue-100 text-blue-700">
                {t}
              </span>
            ))
          )}
        </div>
        {mqttConfig.persisted && mqttConfig.persisted !== mqttConfig.mqtt_telemetry_topic && (
          <p className="text-[11px] text-gray-400 mt-2">
            数据库持久化值: <span className="font-mono">{mqttConfig.persisted}</span>（保存后覆盖）
          </p>
        )}
        <p className="text-[11px] text-gray-400 mt-2">
          支持逗号分隔多个主题，支持 + / # 通配符。保存后后端会取消旧订阅并立即订阅新主题，确保 Neuron 北向 MQTT 数据流入。
        </p>
      </div>

      {/* 级联数据查询 */}
      <DataBrowser />

      {/* 故障码映射表 */}
      <FaultMapManager />

      {/* 告警 HTTP 通知 */}
      <AlarmHttpNotificationPanel />

      {/* nanoMQ 配置与管理 */}
      <NanoMQManager />

      {/* SQL 查询 */}
      <div className="neu-card p-4">
        <h3 className="text-sm font-bold text-gray-800 mb-3">SQL 查询</h3>
        <div className="flex gap-2 mb-3">
          <textarea
            value={sql}
            onChange={(e) => setSql(e.target.value)}
            rows={3}
            className="neu-input flex-1 px-3 py-2 text-xs font-mono"
            placeholder="SELECT * FROM t_telemetry ORDER BY ts DESC LIMIT 100"
          />
          <button
            onClick={handleExecuteSql}
            disabled={sqlLoading}
            className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50 self-start"
          >
            {sqlLoading ? '执行中...' : '执行'}
          </button>
        </div>
        {sqlError && <div className="text-xs text-red-500 mb-2">{sqlError}</div>}
        {sqlResult && (
          <div>
            <div className="text-xs text-gray-500 mb-2">返回 {sqlResult.row_count} 行</div>
            <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-[#f0f0f0]">
                  <tr className="border-b border-gray-200">
                    {sqlResult.columns.map((col) => (
                      <th key={col} className="text-left px-2 py-1 font-medium text-gray-500 text-[11px] uppercase tracking-wider">
                        {col}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sqlResult.rows.map((row, i) => (
                    <tr key={i} className="border-b border-gray-100 hover:bg-white/30">
                      {row.map((cell, j) => (
                        <td key={j} className="px-2 py-1 text-gray-700 font-mono text-[11px]">
                          {cell === null ? 'NULL' : String(cell)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {/* 清空表 */}
      <div className="neu-card p-4 border border-red-200">
        <h3 className="text-sm font-bold text-red-600 mb-3">危险操作：清空表</h3>
        <div className="flex flex-wrap items-center gap-3">
          <select
            value={truncateTableName}
            onChange={(e) => setTruncateTableName(e.target.value)}
            className="neu-input px-3 py-1.5 text-xs bg-transparent"
          >
          <option value="t_telemetry">t_telemetry (遥测数据)</option>
          <option value="t_audit_log">t_audit_log (审计日志)</option>
          </select>
          <input
            type="text"
            value={truncateConfirm}
            onChange={(e) => setTruncateConfirm(e.target.value)}
            placeholder="输入 yes 确认"
            className="neu-input px-3 py-1.5 text-xs w-32"
          />
          <button
            onClick={handleTruncate}
            disabled={truncateLoading || truncateConfirm.toLowerCase() !== 'yes'}
            className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-red-500 hover:bg-red-600 disabled:opacity-50"
          >
            {truncateLoading ? '执行中...' : '清空表'}
          </button>
        </div>
        {truncateMsg && (
          <div className={`text-xs mt-2 ${truncateMsg.includes('已清空') ? 'text-[#389e0d]' : 'text-red-500'}`}>
            {truncateMsg}
          </div>
        )}
        <p className="text-[11px] text-gray-400 mt-2">
          ⚠️ 此操作不可逆，将永久删除表内全部数据。仅用于开发调试。
        </p>
      </div>
    </div>
  )
}
