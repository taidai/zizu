import assert from 'node:assert/strict'
import test from 'node:test'

class MemoryStorage {
  values = new Map()

  getItem(key) { return this.values.get(key) ?? null }
  setItem(key, value) { this.values.set(key, String(value)) }
  removeItem(key) { this.values.delete(key) }
}

test('apply retry survives only for the same actor node plan and digest', async () => {
  const retry = await import('./dataTrunkRetryState.ts')
  const storage = new MemoryStorage()
  const context = {
    actorId: 'engineer-1',
    nodeId: 'pcs-1',
    planId: 'plan-1',
    planDigest: 'a'.repeat(64),
    idempotencyKey: 'apply-key-1',
  }

  retry.saveDataTrunkApplyRetry(storage, context)

  assert.deepEqual(retry.readDataTrunkApplyRetry(storage, context), context)
  assert.equal(
    retry.readDataTrunkApplyRetry(storage, { ...context, actorId: 'engineer-2' }),
    null,
  )
  assert.equal(storage.getItem(retry.DATA_TRUNK_RETRY_STORAGE_KEY), null)

  retry.saveDataTrunkApplyRetry(storage, context)
  assert.equal(
    retry.readDataTrunkApplyRetry(storage, { ...context, planDigest: 'b'.repeat(64) }),
    null,
  )
})

test('invalid persisted retry evidence is removed', async () => {
  const retry = await import('./dataTrunkRetryState.ts')
  const storage = new MemoryStorage()
  storage.setItem(retry.DATA_TRUNK_RETRY_STORAGE_KEY, '{broken')

  assert.equal(retry.readDataTrunkApplyRetry(storage, {
    actorId: 'engineer-1',
    nodeId: 'pcs-1',
    planId: 'plan-1',
    planDigest: 'a'.repeat(64),
  }), null)
  assert.equal(storage.getItem(retry.DATA_TRUNK_RETRY_STORAGE_KEY), null)
})
