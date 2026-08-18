export const DATA_TRUNK_RETRY_STORAGE_KEY = 'zizu.dataTrunk.applyRetry.v1'

export interface DataTrunkApplyRetryContext {
  actorId: string
  nodeId: string
  planId: string
  planDigest: string
  idempotencyKey: string
}

interface RetryStorage {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

type RetryIdentity = Omit<DataTrunkApplyRetryContext, 'idempotencyKey'>

function isRetryContext(value: unknown): value is DataTrunkApplyRetryContext {
  if (!value || typeof value !== 'object') return false
  const context = value as Partial<DataTrunkApplyRetryContext>
  return typeof context.actorId === 'string' && context.actorId.length > 0
    && typeof context.nodeId === 'string' && context.nodeId.length > 0
    && typeof context.planId === 'string' && context.planId.length > 0
    && typeof context.planDigest === 'string' && /^[0-9a-f]{64}$/.test(context.planDigest)
    && typeof context.idempotencyKey === 'string' && context.idempotencyKey.length > 0
}

export function clearDataTrunkApplyRetry(storage: RetryStorage): void {
  try { storage.removeItem(DATA_TRUNK_RETRY_STORAGE_KEY) }
  catch { /* Storage denial must not block the delivery workspace. */ }
}

export function saveDataTrunkApplyRetry(
  storage: RetryStorage,
  context: DataTrunkApplyRetryContext,
): void {
  if (!isRetryContext(context)) return
  try { storage.setItem(DATA_TRUNK_RETRY_STORAGE_KEY, JSON.stringify(context)) }
  catch { /* The in-memory request can still continue. */ }
}

export function readDataTrunkApplyRetry(
  storage: RetryStorage,
  identity: RetryIdentity,
): DataTrunkApplyRetryContext | null {
  try {
    const parsed: unknown = JSON.parse(storage.getItem(DATA_TRUNK_RETRY_STORAGE_KEY) || 'null')
    if (
      !isRetryContext(parsed)
      || parsed.actorId !== identity.actorId
      || parsed.nodeId !== identity.nodeId
      || parsed.planId !== identity.planId
      || parsed.planDigest !== identity.planDigest
    ) {
      clearDataTrunkApplyRetry(storage)
      return null
    }
    return parsed
  } catch {
    clearDataTrunkApplyRetry(storage)
    return null
  }
}

export function findDataTrunkApplyRetry(
  storage: RetryStorage,
  actorId: string,
  nodeId: string,
): DataTrunkApplyRetryContext | null {
  try {
    const parsed: unknown = JSON.parse(storage.getItem(DATA_TRUNK_RETRY_STORAGE_KEY) || 'null')
    if (!isRetryContext(parsed) || parsed.actorId !== actorId || parsed.nodeId !== nodeId) {
      clearDataTrunkApplyRetry(storage)
      return null
    }
    return parsed
  } catch {
    clearDataTrunkApplyRetry(storage)
    return null
  }
}
