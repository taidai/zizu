export type AuthRole = 'admin' | 'engineer' | 'operator'

export interface AuthUser {
  id: string
  username: string
  role: AuthRole
}

export interface AuthSession {
  accessToken: string
  expiresAt: string
  user: AuthUser
}

const STORAGE_KEY = 'zizu.auth.session.v1'
const AUTH_REQUIRED_EVENT = 'zizu:auth-required'

function isAuthRole(value: unknown): value is AuthRole {
  return value === 'admin' || value === 'engineer' || value === 'operator'
}

function isAuthUser(value: unknown): value is AuthUser {
  if (!value || typeof value !== 'object') return false
  const user = value as Partial<AuthUser>
  return typeof user.id === 'string'
    && typeof user.username === 'string'
    && isAuthRole(user.role)
}

function isAuthSession(value: unknown): value is AuthSession {
  if (!value || typeof value !== 'object') return false
  const session = value as Partial<AuthSession>
  return typeof session.accessToken === 'string'
    && session.accessToken.length > 0
    && typeof session.expiresAt === 'string'
    && Number.isFinite(Date.parse(session.expiresAt))
    && isAuthUser(session.user)
}

function removeStoredSession(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // Memory remains the source of truth when browser storage is unavailable.
  }
}

function readStoredSession(): AuthSession | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (!isAuthSession(parsed) || Date.parse(parsed.expiresAt) <= Date.now()) {
      removeStoredSession()
      return null
    }
    return parsed
  } catch {
    removeStoredSession()
    return null
  }
}

let currentSession: AuthSession | null = readStoredSession()

function notifyAuthenticationRequired(): void {
  window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT))
}

export function getAuthSession(): AuthSession | null {
  if (currentSession && Date.parse(currentSession.expiresAt) <= Date.now()) {
    currentSession = null
    removeStoredSession()
    queueMicrotask(notifyAuthenticationRequired)
  }
  return currentSession
}

export function setAuthSession(session: AuthSession): void {
  currentSession = session
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session))
  } catch {
    // The in-memory session still supports this tab when storage is unavailable.
  }
}

export function updateAuthUser(user: AuthUser): void {
  const session = getAuthSession()
  if (session) setAuthSession({ ...session, user })
}

/** Clear only the session that initiated a request, never a newer login. */
export function clearAuthSession(expectedToken?: string): boolean {
  if (expectedToken && currentSession?.accessToken !== expectedToken) return false
  if (!currentSession) {
    removeStoredSession()
    return false
  }
  currentSession = null
  removeStoredSession()
  return true
}

export function invalidateAuthSession(requestToken: string): void {
  if (clearAuthSession(requestToken)) notifyAuthenticationRequired()
}

export function subscribeAuthenticationRequired(listener: () => void): () => void {
  window.addEventListener(AUTH_REQUIRED_EVENT, listener)
  return () => window.removeEventListener(AUTH_REQUIRED_EVENT, listener)
}
