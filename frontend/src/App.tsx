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

const NodeTreePage = lazy(() => import('./pages/NodeTreePage'))
const RuleEnginePage = lazy(() => import('./pages/RuleEnginePage'))
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
  if (!health) return null
  const p = health.pipeline
  const isOk = p.status.toLowerCase() === 'running' && health.components.mqtt.status === 'connected'

  return (
    <div className="neu-card px-4 py-2 flex items-center gap-6 text-xs">
      <div className="flex items-center">
        <span className={`status-dot ${isOk ? 'ok' : 'error'}`} />
        <span className="font-medium">{isOk ? 'Pipeline 运行中' : 'Pipeline 异常'}</span>
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
      <div className="ml-auto text-gray-400">v{health.version}</div>
    </div>
  )
}

type PageKey = 'workbench' | 'tree' | 'rules' | 'alarms' | 'admin'

const ROLE_LABELS: Record<AuthRole, string> = {
  admin: '平台管理员',
  engineer: '实施工程师',
  operator: '业主操作员',
}

interface NavigationItem {
  key: PageKey
  label: string
  operatorLabel?: string
  icon: React.ReactNode
  roles: AuthRole[]
}

const ALL_ROLES: AuthRole[] = ['admin', 'engineer', 'operator']
const CONFIG_ROLES: AuthRole[] = ['admin', 'engineer']

const NAV_ITEMS: NavigationItem[] = [
  { key: 'workbench', label: 'EMS 工作台', icon: <LayoutDashboard size={18} strokeWidth={1.8} />, roles: ALL_ROLES },
  { key: 'tree', label: '节点管理', operatorLabel: '运行监控', icon: <Network size={18} strokeWidth={1.8} />, roles: ALL_ROLES },
  { key: 'alarms', label: '告警中心', icon: <Bell size={18} strokeWidth={1.8} />, roles: ALL_ROLES },
  { key: 'rules', label: '规则引擎', icon: <Scale size={18} strokeWidth={1.8} />, roles: CONFIG_ROLES },
  // Server-side protection for system/control APIs is intentionally Ticket #4.
  { key: 'admin', label: '系统工具', icon: <Settings size={18} strokeWidth={1.8} />, roles: ['admin'] },
]

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
    <div className="min-h-screen bg-[#e8e8e8] flex items-center justify-center p-6">
      <form onSubmit={handleSubmit} className="neu-card w-full max-w-sm p-8 space-y-5">
        <div>
          <h1 className="text-2xl font-bold text-gray-800">ZiZu</h1>
          <p className="mt-1 text-xs text-gray-500">工业控制系统交付与运行平台</p>
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
          className="w-full rounded-lg bg-[#52c41a] px-4 py-2.5 text-sm font-medium text-white shadow disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? '正在登录...' : '登录'}
        </button>
        <p className="text-[11px] leading-5 text-gray-400">账号由平台管理员线下供应，系统不提供默认账号或密码。</p>
      </form>
    </div>
  )
}

function AuthenticatedApp({ session, onLoggedOut }: { session: AuthSession; onLoggedOut: () => void }) {
  const [activePage, setActivePage] = useState<PageKey>('workbench')
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const healthRequestGenerationRef = useRef(0)
  const [collapsed, setCollapsed] = useState(false)
  const [loggingOut, setLoggingOut] = useState(false)
  const navigation = useMemo(
    () => NAV_ITEMS.filter((item) => item.roles.includes(session.user.role)),
    [session.user.role],
  )

  useEffect(() => {
    if (!navigation.some((item) => item.key === activePage)) {
      setActivePage(navigation[0]?.key || 'alarms')
    }
  }, [activePage, navigation])

  const loadHealth = useCallback(async () => {
    const generation = ++healthRequestGenerationRef.current
    try {
      const nextHealth = await fetchHealth()
      if (generation === healthRequestGenerationRef.current) setHealth(nextHealth)
    } catch {
      // Keep the last known state; the next poll or manual refresh retries.
    }
  }, [])

  useEffect(() => {
    void loadHealth()
    const id = setInterval(() => { void loadHealth() }, 5000)
    return () => clearInterval(id)
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

  return (
    <div className="min-h-screen bg-[#e8e8e8] flex">
      {/* 侧边栏 */}
      <aside
        className={`neu-card m-4 mr-0 p-3 flex flex-col transition-all duration-300 ${
          collapsed ? 'w-16 items-center' : 'w-56'
        }`}
      >
        <div className={`flex items-center ${collapsed ? 'mb-4 justify-center' : 'mb-6 px-2 pt-1 justify-between'}`}>
          {!collapsed && (
            <div>
              <h1 className="text-lg font-bold text-gray-800">ZiZu</h1>
              <p className="text-[10px] text-gray-400">工业 IoT 平台</p>
            </div>
          )}
          <button
            onClick={() => setCollapsed(!collapsed)}
            title={collapsed ? '展开' : '收起'}
            className="neu-btn w-7 h-7 flex items-center justify-center text-xs text-gray-500 hover:text-gray-700"
          >
            {collapsed ? '▶' : '◀'}
          </button>
        </div>
        <nav className={`space-y-2 w-full ${collapsed ? 'flex flex-col items-center' : ''}`}>
          {navigation.map((item) => {
            const label = session.user.role === 'operator' && item.operatorLabel ? item.operatorLabel : item.label
            return (
            <button
              key={item.key}
              onClick={() => setActivePage(item.key)}
              title={collapsed ? label : undefined}
              className={`flex items-center rounded-xl text-sm font-medium transition-colors ${
                collapsed ? 'w-10 h-10 justify-center px-0' : 'w-full gap-3 px-3 py-2.5'
              } ${
                activePage === item.key
                  ? 'bg-[#52c41a] text-white shadow'
                  : 'text-gray-600 hover:bg-white/40'
              }`}
            >
              <span className="shrink-0">{item.icon}</span>
              {!collapsed && <span className="truncate">{label}</span>}
            </button>
            )
          })}
        </nav>
        <div className={`mt-auto ${collapsed ? 'text-center' : 'px-1 pb-1'}`}>
          {!collapsed && (
            <div className="mb-3 rounded-xl border border-white/60 bg-white/30 px-3 py-2">
              <div className="truncate text-xs font-medium text-gray-700" title={session.user.username}>{session.user.username}</div>
              <div className="mt-0.5 text-[10px] text-gray-500">{ROLE_LABELS[session.user.role]}</div>
              <button
                onClick={handleLogout}
                disabled={loggingOut}
                className="mt-2 text-[11px] text-gray-500 hover:text-red-600 disabled:opacity-50"
              >
                {loggingOut ? '正在退出...' : '退出登录'}
              </button>
            </div>
          )}
          {collapsed && (
            <button
              onClick={handleLogout}
              disabled={loggingOut}
              title={`${session.user.username} · ${ROLE_LABELS[session.user.role]} · 退出登录`}
              className="neu-btn mb-3 h-8 w-8 text-xs font-medium text-gray-600 disabled:opacity-50"
            >
              {session.user.username.slice(0, 1).toUpperCase()}
            </button>
          )}
          <div className="text-[10px] text-gray-400">FE {__APP_VERSION__}</div>
        </div>
      </aside>

      {/* 主内容 */}
      <main className="flex-1 p-6 overflow-auto min-w-0">
        {!session.accessToken && (
          <div role="alert" className="mb-4 rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-xs font-semibold text-red-800">
            不安全开发模式：当前会话未认证。不得将此实例暴露到生产网络。
          </div>
        )}
        <PipelineBar health={health} />
        <div className="mt-4">
          <Suspense fallback={<PageLoader />}>
            {activePage === 'workbench' && <EMSWorkbenchPage onOpenAlarms={() => setActivePage('alarms')} />}
            {activePage === 'tree' && (
              <NodeTreePage
                actorId={session.user.id}
                readOnly={session.user.role === 'operator'}
                canManageTemplates={session.user.role === 'admin'}
                health={health}
                onRefreshHealth={loadHealth}
              />
            )}
            {activePage === 'rules' && <RuleEnginePage />}
            {activePage === 'alarms' && <AlarmCenterPage actorId={session.user.id} canConfigure={CONFIG_ROLES.includes(session.user.role)} />}
          </Suspense>
          {activePage === 'admin' && <AdminPanel />}
        </div>
      </main>
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
          <button onClick={() => void restoreSession()} className="mt-5 rounded-lg bg-[#52c41a] px-4 py-2 text-xs font-medium text-white">重试</button>
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
