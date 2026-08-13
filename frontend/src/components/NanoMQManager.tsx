import { useEffect, useState } from 'react'
import {
  fetchNanoMQStatus, fetchNanoMQClients, fetchNanoMQSubscriptions,
  fetchNanoMQACL, updateNanoMQACL,
  fetchNanoMQConfig, updateNanoMQConfig, restartNanoMQ,
  type NanoMQACLRule,
} from '../api/client'

type TabKey = 'overview' | 'clients' | 'subscriptions' | 'acl' | 'config'

export default function NanoMQManager() {
  const [activeTab, setActiveTab] = useState<TabKey>('overview')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // overview
  const [status, setStatus] = useState<any>(null)
  // clients
  const [clients, setClients] = useState<any[]>([])
  // subscriptions
  const [subscriptions, setSubscriptions] = useState<any[]>([])
  // acl
  const [aclRules, setAclRules] = useState<NanoMQACLRule[]>([])
  const [aclSaving, setAclSaving] = useState(false)
  // config
  const [configContent, setConfigContent] = useState('')
  const [configPath, setConfigPath] = useState('')
  const [configSaving, setConfigSaving] = useState(false)
  const [configMsg, setConfigMsg] = useState('')
  const [restartLoading, setRestartLoading] = useState(false)

  const loadStatus = async () => {
    try {
      const data = await fetchNanoMQStatus()
      setStatus(data)
      if (data.error) setError(`nanoMQ API 错误: ${JSON.stringify(data.message)}`)
      else setError('')
    } catch (e: any) {
      setError(`连接 nanoMQ API 失败: ${e.message}`)
      setStatus(null)
    }
  }

  const loadClients = async () => {
    setLoading(true)
    try {
      const data = await fetchNanoMQClients()
      setClients(data.data || [])
    } catch (e: any) {
      setError(`获取客户端失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  const loadSubscriptions = async () => {
    setLoading(true)
    try {
      const data = await fetchNanoMQSubscriptions()
      setSubscriptions(data.data || [])
    } catch (e: any) {
      setError(`获取订阅失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  const loadACL = async () => {
    setLoading(true)
    try {
      const data = await fetchNanoMQACL()
      setAclRules(data.data || [])
    } catch (e: any) {
      setError(`获取 ACL 失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  const loadConfig = async () => {
    setLoading(true)
    try {
      const data = await fetchNanoMQConfig()
      setConfigContent(data.content)
      setConfigPath(data.path)
    } catch (e: any) {
      setError(`获取配置失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadStatus()
    const id = setInterval(loadStatus, 5000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    if (activeTab === 'clients') loadClients()
    if (activeTab === 'subscriptions') loadSubscriptions()
    if (activeTab === 'acl') loadACL()
    if (activeTab === 'config') loadConfig()
  }, [activeTab])

  const handleSaveACL = async () => {
    setAclSaving(true)
    try {
      await updateNanoMQACL(aclRules)
      setError('')
    } catch (e: any) {
      setError(`保存 ACL 失败: ${e.message}`)
    } finally {
      setAclSaving(false)
    }
  }

  const handleAddACL = () => {
    setAclRules([...aclRules, { action: 'pub', permit: 'allow', topic: '#' }])
  }

  const handleUpdateACL = (idx: number, field: keyof NanoMQACLRule, value: string) => {
    const next = [...aclRules]
    next[idx] = { ...next[idx], [field]: value || undefined }
    setAclRules(next)
  }

  const handleDeleteACL = (idx: number) => {
    setAclRules(aclRules.filter((_, i) => i !== idx))
  }

  const handleSaveConfig = async () => {
    setConfigSaving(true)
    setConfigMsg('')
    try {
      await updateNanoMQConfig(configContent)
      setConfigMsg('配置已保存，重启 nanoMQ 后生效')
    } catch (e: any) {
      setConfigMsg(`保存失败: ${e.message}`)
    } finally {
      setConfigSaving(false)
    }
  }

  const handleRestart = async () => {
    setRestartLoading(true)
    setConfigMsg('')
    try {
      const res = await restartNanoMQ()
      setConfigMsg(res.message)
    } catch (e: any) {
      setConfigMsg(`重启失败: ${e.message}`)
    } finally {
      setRestartLoading(false)
    }
  }

  const tabs: { key: TabKey; label: string }[] = [
    { key: 'overview', label: '概览' },
    { key: 'clients', label: '客户端' },
    { key: 'subscriptions', label: '订阅' },
    { key: 'acl', label: 'ACL' },
    { key: 'config', label: '配置文件' },
  ]

  const renderOverview = () => {
    if (!status) return <div className="text-sm text-gray-500">正在加载状态...</div>
    if (status.error) return <div className="text-sm text-red-500">nanoMQ API 暂不可用</div>
    const broker = status.brokers?.data?.[0] || status.brokers || {}
    const metrics = status.metrics?.data || status.metrics || {}
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <KV label="版本" value={broker.sysdescr || broker.version || '-'} />
          <KV label="运行时间" value={broker.uptime || '-'} />
          <KV label="连接数" value={metrics.connections || metrics.clients || '-'} />
          <KV label="订阅数" value={metrics.subscriptions || '-'} />
        </div>
        <div className="text-[11px] text-gray-400">
          路径: /api/v1/nanomq/status · 每 5 秒自动刷新
        </div>
      </div>
    )
  }

  return (
    <div className="neu-card p-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-bold text-gray-800">nanoMQ 管理</h3>
        <button
          onClick={loadStatus}
          className="neu-btn px-3 py-1 text-xs font-medium text-gray-600"
        >
          刷新
        </button>
      </div>

      {error && <div className="text-xs text-red-500 mb-3">{error}</div>}

      <div className="flex gap-2 mb-4 overflow-x-auto pb-1">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setActiveTab(t.key)}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition-colors ${
              activeTab === t.key
                ? 'bg-[#52c41a] text-white'
                : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {activeTab === 'overview' && renderOverview()}

      {activeTab === 'clients' && (
        <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[#f0f0f0]">
              <tr className="border-b border-gray-200">
                <Th>客户端 ID</Th>
                <Th>用户名</Th>
                <Th>IP</Th>
                <Th>协议</Th>
                <Th>连接时间</Th>
              </tr>
            </thead>
            <tbody>
              {clients.length === 0 ? (
                <tr><td colSpan={5} className="px-2 py-4 text-center text-gray-400">暂无客户端</td></tr>
              ) : (
                clients.map((c, i) => (
                  <tr key={i} className="border-b border-gray-100 hover:bg-white/30">
                    <Td>{c.client_id || c.clientid || '-'}</Td>
                    <Td>{c.username || '-'}</Td>
                    <Td>{c.ipaddress || c.ip_address || '-'}</Td>
                    <Td>{c.proto_name || '-'}</Td>
                    <Td>{c.connected_at || '-'}</Td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {activeTab === 'subscriptions' && (
        <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[#f0f0f0]">
              <tr className="border-b border-gray-200">
                <Th>客户端 ID</Th>
                <Th>主题</Th>
                <Th>QoS</Th>
              </tr>
            </thead>
            <tbody>
              {subscriptions.length === 0 ? (
                <tr><td colSpan={3} className="px-2 py-4 text-center text-gray-400">暂无订阅</td></tr>
              ) : (
                subscriptions.map((s, i) => (
                  <tr key={i} className="border-b border-gray-100 hover:bg-white/30">
                    <Td>{s.clientid || s.client_id || '-'}</Td>
                    <Td>{s.topic || '-'}</Td>
                    <Td>{s.qos ?? '-'}</Td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {activeTab === 'acl' && (
        <div className="space-y-3">
          <div className="overflow-x-auto max-h-[300px] overflow-y-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-[#f0f0f0]">
                <tr className="border-b border-gray-200">
                  <Th>动作</Th>
                  <Th>权限</Th>
                  <Th>用户名</Th>
                  <Th>客户端 ID</Th>
                  <Th>IP</Th>
                  <Th>主题</Th>
                  <Th></Th>
                </tr>
              </thead>
              <tbody>
                {aclRules.map((rule, i) => (
                  <tr key={i} className="border-b border-gray-100">
                    <Td>
                      <select
                        className="neu-input px-1 py-0.5 text-xs bg-transparent"
                        value={rule.action}
                        onChange={(e) => handleUpdateACL(i, 'action', e.target.value)}
                      >
                        <option value="pub">pub</option>
                        <option value="sub">sub</option>
                        <option value="all">all</option>
                      </select>
                    </Td>
                    <Td>
                      <select
                        className="neu-input px-1 py-0.5 text-xs bg-transparent"
                        value={rule.permit}
                        onChange={(e) => handleUpdateACL(i, 'permit', e.target.value)}
                      >
                        <option value="allow">allow</option>
                        <option value="deny">deny</option>
                      </select>
                    </Td>
                    <Td><input className="neu-input px-1 py-0.5 text-xs w-24" value={rule.username || ''} onChange={(e) => handleUpdateACL(i, 'username', e.target.value)} /></Td>
                    <Td><input className="neu-input px-1 py-0.5 text-xs w-24" value={rule.clientid || ''} onChange={(e) => handleUpdateACL(i, 'clientid', e.target.value)} /></Td>
                    <Td><input className="neu-input px-1 py-0.5 text-xs w-24" value={rule.ipaddr || ''} onChange={(e) => handleUpdateACL(i, 'ipaddr', e.target.value)} /></Td>
                    <Td><input className="neu-input px-1 py-0.5 text-xs w-32" value={rule.topic} onChange={(e) => handleUpdateACL(i, 'topic', e.target.value)} /></Td>
                    <Td>
                      <button onClick={() => handleDeleteACL(i)} className="text-red-500 hover:text-red-700 text-xs">删除</button>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex gap-2">
            <button onClick={handleAddACL} className="neu-btn px-3 py-1 text-xs">新增规则</button>
            <button
              onClick={handleSaveACL}
              disabled={aclSaving}
              className="neu-btn px-4 py-1 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50"
            >
              {aclSaving ? '保存中...' : '保存 ACL'}
            </button>
          </div>
        </div>
      )}

      {activeTab === 'config' && (
        <div className="space-y-3">
          <div className="text-[11px] text-gray-500">路径: {configPath || '加载中...'}</div>
          <textarea
            className="neu-input w-full px-3 py-2 text-xs font-mono"
            rows={16}
            value={configContent}
            onChange={(e) => setConfigContent(e.target.value)}
          />
          <div className="flex items-center gap-3 flex-wrap">
            <button
              onClick={handleSaveConfig}
              disabled={configSaving}
              className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50"
            >
              {configSaving ? '保存中...' : '保存配置'}
            </button>
            <button
              onClick={handleRestart}
              disabled={restartLoading}
              className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 disabled:opacity-50"
            >
              {restartLoading ? '重启中...' : '保存并重启 nanoMQ'}
            </button>
            {configMsg && <span className="text-xs text-gray-500">{configMsg}</span>}
          </div>
          <p className="text-[11px] text-gray-400">
            修改 HOCON 配置文件后保存，再点击“保存并重启 nanoMQ”使配置生效。重启前会自动备份原配置。
          </p>
        </div>
      )}
    </div>
  )
}

function KV({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="bg-white/40 rounded-lg p-3">
      <div className="text-[11px] text-gray-500 mb-1">{label}</div>
      <div className="text-sm font-medium text-gray-800 break-all">{value}</div>
    </div>
  )
}

function Th({ children }: { children?: React.ReactNode }) {
  return (
    <th className="text-left px-2 py-1.5 font-medium text-gray-500 text-[11px] uppercase tracking-wider">
      {children}
    </th>
  )
}

function Td({ children }: { children?: React.ReactNode }) {
  return <td className="px-2 py-1.5 text-gray-700 text-[11px]">{children}</td>
}
