import assert from 'node:assert/strict'
import test from 'node:test'

class MemoryStorage {
  values = new Map()

  getItem(key) { return this.values.get(key) ?? null }
  setItem(key, value) { this.values.set(key, String(value)) }
  removeItem(key) { this.values.delete(key) }
}

test('acceptance retry survives only for the same actor and application', async () => {
  const contracts = await import('./acceptanceRetryState.ts')
  const storage = new MemoryStorage()
  const saved = {
    actorId: 'user-engineer-a',
    applicationId: 'application-a',
    idempotencyKey: 'stable-key',
  }

  contracts.saveAcceptanceRetry(storage, saved)

  assert.deepEqual(
    contracts.readAcceptanceRetry(storage, 'user-engineer-a', 'application-a'),
    saved,
  )
  assert.equal(
    contracts.readAcceptanceRetry(storage, 'user-engineer-a', 'application-b'),
    null,
  )
  assert.equal(storage.getItem(contracts.ACCEPTANCE_RETRY_STORAGE_KEY), null)

  contracts.saveAcceptanceRetry(storage, saved)
  assert.equal(
    contracts.readAcceptanceRetry(storage, 'user-engineer-b', 'application-a'),
    null,
  )
})

test('logout clearing prevents the same actor from reusing an old key', async () => {
  const contracts = await import('./acceptanceRetryState.ts')
  const storage = new MemoryStorage()
  const saved = {
    actorId: 'user-engineer-a',
    applicationId: 'application-a',
    idempotencyKey: 'stable-key',
  }
  contracts.saveAcceptanceRetry(storage, saved)

  contracts.clearAcceptanceRetry(storage)

  assert.equal(
    contracts.readAcceptanceRetry(storage, 'user-engineer-a', 'application-a'),
    null,
  )
})

test('successful normal and restored apply use the same ordered refresh contract', async () => {
  const contracts = await import('./acceptanceRetryState.ts')
  const calls = []

  await contracts.refreshAppliedWorkspace(
    async () => { calls.push('core') },
    async () => { calls.push('acceptance') },
  )

  assert.deepEqual(calls, ['core', 'acceptance'])
})
