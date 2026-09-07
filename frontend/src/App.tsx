import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { fetchCurrentUser, fetchHealth, login, logout, type HealthStatus } from './api/client'
import {
  clearAuthSession,
  getAuthSession,
  subscribeAuthenticationRequired,
  type AuthRole,
  type AuthSession,
} from './api/authSession'
import AdminPanel from './components/AdminPanel'
import { clearDataTrunkApplyRetry } from './components/data-trunk/dataTrunkRetryState'
import { Network, Scale, Bell, Settings, LayoutDashboard } from 'lucide-react'
import { pagesForArea, resolveTabletPage, type TabletArea, type TabletPage } from './appNavigationModel'

const NodeTreePage = lazy(() => import('./pages/NodeTreePage'))
const DeviceMonitorPage = lazy(() => import('./pages/DeviceMonitorPage'))
const DispatchStrategyPage = lazy(() => import('./pages/DispatchStrategyPage'))
const AlarmCenterPage = lazy(() => import('./pages/AlarmCenterPage'))
const EMSWorkbenchPage = lazy(() => import('./pages/EMSWorkbenchPage'))

function PageLoader() {
  return (
    <div className="neu-card p-8 flex items-center justify-center text-sm text-gray-500">
      页面加载中...
    </div>
  )
}

function PipelineBar({ health }: { health: HealthStatus | null }) {
  if (!health) return <div role="status" aria-label="数据链路状态" className="zizu-pipeline"><span className="status-dot warn" /><span>连接未知 · 暂未取得平台状态</span><span className="ml-auto">FE {__APP_VERSION__}</span></div>
  const p = health.pipeline
  const isOk = p.status.toLowerCase() === 'running' && health.components.mqtt.status === 'connected'

  return (
    <div role="status" aria-label="数据链路状态" className="zizu-pipeline">
      <div className="flex items-center">
        <span className={`status-dot ${isOk ? 'ok' : 'error'}`} />
        <span className="font-medium">{isOk ? '采集运行中' : '采集异常'}</span>
      </div>
      <div className="text-gray-500">
        消息: <span className="font-mono-value">{p.messages_received.toLocaleString()}</span>
      </div>
      <div className="text-gray-500">
        入库: <span className="font-mono-value">{p.points_written_db.toLocaleString()}</span>
      </div>
      <div className="text-gray-500">
        MQTT: <span className={health.components.mqtt.status === 'connected' ? 'text-green-600' : 'text-red-500'}>{health.components.mqtt.status}</span>
      </div>
      <div className="text-gray-500">
        最后消息: {p.last_message_at ? new Date(p.last_message_at).toLocaleTimeString() : '—'}
      </div>
      <div className="ml-auto text-gray-400">FE {__APP_VERSION__} · API {health.version}</div>
    </div>
  )
}

const ROLE_LABELS: Record<AuthRole, string> = {
  admin: '平台管理员',
  engineer: '实施工程师',
  operator: '业主操作员',
}

const CONFIG_ROLES: AuthRole[] = ['admin', 'engineer']
const NAV_ITEMS: Record<TabletPage, { label: string; icon: React.ReactNode }> = {
  workbench: { label: '总览', icon: <LayoutDashboard size={20} strokeWidth={1.8} /> },
  monitor: { label: '设备监控', icon: <Network size={20} strokeWidth={1.8} /> },
  tree: { label: '节点与数据', icon: <Network size={20} strokeWidth={1.8} /> },
  alarms: { label: '告警', icon: <Bell size={20} strokeWidth={1.8} /> },
  strategies: { label: '调度策略', icon: <Scale size={20} strokeWidth={1.8} /> },
  controls: { label: '手动控制', icon: <Scale size={20} strokeWidth={1.8} /> },
  admin: { label: '系统工具', icon: <Settings size={20} strokeWidth={1.8} /> },
}

function LoginGate({ onAuthenticated }: { onAuthenticated: (session: AuthSession) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    if (!username.trim() || !password) return
    setSubmitting(true)
    setError('')
    try {
      onAuthenticated(await login(username.trim(), password))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '登录失败，请重试')
    } finally {
      setPassword('')
      setSubmitting(false)
    }
  }

  return (
    <div className="zizu-auth-screen">
      <form onSubmit={handleSubmit} className="neu-card zizu-login w-full max-w-sm p-8 space-y-5">
        <div>
          <h1 className="text-2xl font-bold">自足IOT</h1>
          <p className="mt-1 text-xs text-gray-500">简单配置，交付光储充 EMS</p>
        </div>
        <div className="space-y-3">
          <label className="block text-xs font-medium text-gray-600">
            用户名
            <input
              autoFocus
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              className="neu-input mt-1.5 w-full px-3 py-2.5 text-sm"
              maxLength={128}
            />
          </label>
          <label className="block text-xs font-medium text-gray-600">
            密码
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              className="neu-input mt-1.5 w-full px-3 py-2.5 text-sm"
            />
          </label>
        </div>
        {error && (
          <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
            {error}
          </div>
        )}
        <button
          type="submit"
          disabled={submitting || !username.trim() || !password}
          className="zizu-primary w-full rounded-lg px-4 py-2.5 text-sm font-medium shadow disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? '正在登录...' : '登录'}
        </button>
        <p className="text-[11px] leading-5 text-gray-400">账号由平台管理员线下供应，系统不提供默认账号或密码。</p>
      </form>
    </div>
  )
}

function AuthenticatedApp({ session, onLoggedOut }: { session: AuthSession; onLoggedOut: () => void }) {
  const [activePage, setActivePage] = useState<TabletPage>('workbench')
  const [area, setArea] = useState<TabletArea>('runtime')
  const [targetNodeId, setTargetNodeId] = useState<string | undefined>()
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const healthRequestGenerationRef = useRef(0)
  const [loggingOut, setLoggingOut] = useState(false)
  const canConfigure = CONFIG_ROLES.includes(session.user.role)
  const safeArea = canConfigure ? area : 'runtime'
  const safePage = resolveTabletPage(session.user.role, activePage)
  const navigation = useMemo(() => pagesForArea(session.user.role, safeArea), [session.user.role, safeArea])
  const navigate = (page: TabletPage, nextArea: TabletArea = safeArea) => {
    setActivePage(resolveTabletPage(session.user.role, page))
    setArea(canConfigure ? nextArea : 'runtime')
  }
  const openEngineering = (nodeId?: string) => {
    if (!canConfigure) return
    setTargetNodeId(nodeId)
    navigate('tree', 'engineering')
  }

  const loadHealth = useCallback(async () => {
    const generation = ++healthRequestGenerationRef.current
    try {
      const nextHealth = await fetchHealth()
      if (generation === healthRequestGenerationRef.current) setHealth(nextHealth)
    } catch {
      if (generation === healthRequestGenerationRef.current) setHealth(null)
    }
  }, [])

  useEffect(() => {
    void loadHealth()
    const id = setInterval(() => { void loadHealth() }, 5000)
    return () => { clearInterval(id); healthRequestGenerationRef.current += 1 }
  }, [loadHealth])

  const handleLogout = async () => {
    setLoggingOut(true)
    try {
      await logout()
    } catch {
      // Local logout is authoritative even when the server is unavailable.
    } finally {
      clearDataTrunkApplyRetry(sessionStorage)
      onLoggedOut()
    }
  }

  const runtimeProps = {
    onOpenAlarms: () => navigate('alarms', 'runtime'),
    onOpenEngineering: canConfigure ? openEngineering : undefined,
    onOpenDevices: () => navigate('monitor', 'runtime'),
    initialTab: safePage === 'controls' ? 'controls' as const : 'overview' as const,
  }
  const nodeProps = { initialNodeId: targetNodeId }
  const navigationButtons = navigation.map((page) => (
    <button key={page} type="button" aria-current={safePage === page ? 'page' : undefined}
      onClick={() => navigate(page)} className={`neu-btn zizu-nav-button ${safePage === page ? 'zizu-tab-active' : ''}`}>
      {NAV_ITEMS[page].icon}<span>{NAV_ITEMS[page].label}</span>
    </button>
  ))

  return (
    <div className="zizu-shell">
      <a className="zizu-skip-link" href="#zizu-main">跳转到主要内容</a>
      <header className="zizu-header">
        <h1 className="zizu-brand">自足<span>IOT</span></h1>
        <div className="zizu-header-title"><strong>{safeArea === 'engineering' ? '工程配置' : '光储充现场'}</strong><span>简单配置，交付光储充 EMS</span></div>
        <div className="zizu-account"><span title={session.user.username}>{session.user.username}</span><small>{ROLE_LABELS[session.user.role]}</small></div>
        {canConfigure && <button type="button" className="zizu-header-action" onClick={() => safeArea === 'engineering' ? navigate('workbench', 'runtime') : openEngineering()}><Settings size={19} />{safeArea === 'engineering' ? '返回现场' : '工程配置'}</button>}
        <button type="button" onClick={handleLogout} disabled={loggingOut} className="zizu-logout">{loggingOut ? '正在退出...' : '退出登录'}</button>
      </header>
      <PipelineBar health={health} />
      {safeArea === 'engineering' && <nav aria-label="工程配置导航" className="zizu-engineering-nav">{navigationButtons}</nav>}
      <main id="zizu-main" tabIndex={-1} className={`zizu-main ${safeArea === 'engineering' ? 'zizu-engineering-main' : ''}`}>
        {!session.accessToken && (
          <div role="alert" className="mb-4 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-xs font-semibold text-red-800">
            不安全开发模式：当前会话未认证。不得将此实例暴露到生产网络。
          </div>
        )}
        <div className="zizu-page">
          <Suspense fallback={<PageLoader />}>
            {(safePage === 'workbench' || safePage === 'controls') && <EMSWorkbenchPage key={safePage} {...runtimeProps} />}
            {safePage === 'tree' && (
              <NodeTreePage
                {...nodeProps}
                actorId={session.user.id}
                readOnly={session.user.role === 'operator'}
                canManageTemplates={session.user.role === 'admin'}
                health={health}
                onRefreshHealth={loadHealth}
              />
            )}
            {safePage === 'monitor' && (
              <DeviceMonitorPage onOpenEngineering={canConfigure ? openEngineering : undefined} />
            )}
            {safePage === 'strategies' && <DispatchStrategyPage />}
            {safePage === 'alarms' && <AlarmCenterPage actorId={session.user.id} canConfigure={canConfigure} />}
          </Suspense>
          {safePage === 'admin' && <AdminPanel />}
        </div>
      </main>
      {safeArea === 'runtime' && <nav aria-label="日常运行" className="zizu-runtime-nav">{navigationButtons}</nav>}
    </div>
  )
}

export default function App() {
  const [session, setSession] = useState<AuthSession | null>(null)
  const [restoring, setRestoring] = useState(true)
  const [restoreError, setRestoreError] = useState('')

  const restoreSession = useCallback(async () => {
    const stored = getAuthSession()
    if (!stored) {
      try {
        const user = await fetchCurrentUser()
        setSession({ accessToken: '', expiresAt: new Date(Date.now() + 60 * 60 * 1000).toISOString(), user })
      } catch {
        setSession(null)
      }
      setRestoreError('')
      setRestoring(false)
      return
    }

    setSession(null)
    setRestoring(true)
    setRestoreError('')
    try {
      const user = await fetchCurrentUser()
      const current = getAuthSession()
      if (current) setSession({ ...current, user })
    } catch {
      if (getAuthSession()) setRestoreError('暂时无法验证登录会话，请检查平台连接后重试。')
      setSession(null)
    } finally {
      setRestoring(false)
    }
  }, [])

  useEffect(() => {
    const unsubscribe = subscribeAuthenticationRequired(() => {
      clearDataTrunkApplyRetry(sessionStorage)
      setSession(null)
      setRestoreError('')
      setRestoring(false)
    })
    void restoreSession()
    return unsubscribe
  }, [restoreSession])

  if (restoring) {
    return <div className="min-h-screen bg-[#e8e8e8] flex items-center justify-center text-sm text-gray-500">正在验证登录会话...</div>
  }

  if (restoreError) {
    return (
      <div className="min-h-screen bg-[#e8e8e8] flex items-center justify-center p-6">
        <div className="neu-card w-full max-w-sm p-8 text-center">
          <h1 className="text-base font-bold text-gray-800">平台连接不可用</h1>
          <p className="mt-2 text-xs leading-5 text-gray-500">{restoreError}</p>
          <button onClick={() => void restoreSession()} className="zizu-primary mt-5 rounded-lg px-4 py-2 text-xs font-medium text-white">重试</button>
          <button
            onClick={() => {
              clearDataTrunkApplyRetry(sessionStorage)
              clearAuthSession()
              setRestoreError('')
              setSession(null)
            }}
            className="ml-3 px-3 py-2 text-xs text-gray-500 hover:text-red-600"
          >
            清除本地会话
          </button>
        </div>
      </div>
    )
  }

  if (!session) return <LoginGate onAuthenticated={setSession} />
  return <AuthenticatedApp session={session} onLoggedOut={() => setSession(null)} />
}
