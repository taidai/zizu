export const ACCEPTANCE_RETRY_STORAGE_KEY = 'zizu.alarmConfiguration.acceptanceRetry'

export interface AcceptanceRetryContext {
  actorId: string
  applicationId: string
  idempotencyKey: string
}

interface RetryStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

const isRetryContext = (value: unknown): value is AcceptanceRetryContext => {
  if (!value || typeof value !== 'object') return false
  const context = value as Partial<AcceptanceRetryContext>
  return typeof context.actorId === 'string'
    && context.actorId.length > 0
    && typeof context.applicationId === 'string'
    && context.applicationId.length > 0
    && typeof context.idempotencyKey === 'string'
    && context.idempotencyKey.length > 0
}

export function clearAcceptanceRetry(storage: RetryStorage): void {
  try { storage.removeItem(ACCEPTANCE_RETRY_STORAGE_KEY) }
  catch { /* Storage denial must not block the acceptance workspace. */ }
}

export function readAcceptanceRetry(
  storage: RetryStorage,
  actorId: string,
  applicationId: string,
): AcceptanceRetryContext | null {
  try {
    const value: unknown = JSON.parse(
      storage.getItem(ACCEPTANCE_RETRY_STORAGE_KEY) || 'null',
    )
    if (
      !isRetryContext(value)
      || value.actorId !== actorId
      || value.applicationId !== applicationId
    ) {
      clearAcceptanceRetry(storage)
      return null
    }
    return value
  } catch {
    clearAcceptanceRetry(storage)
    return null
  }
}

export function saveAcceptanceRetry(
  storage: RetryStorage,
  context: AcceptanceRetryContext,
): void {
  try {
    storage.setItem(ACCEPTANCE_RETRY_STORAGE_KEY, JSON.stringify(context))
  } catch { /* The in-memory key remains valid when storage is unavailable. */ }
}

export async function refreshAppliedWorkspace(
  loadCore: () => Promise<void>,
  loadAcceptance: () => Promise<void>,
): Promise<void> {
  await loadCore()
  await loadAcceptance()
}
