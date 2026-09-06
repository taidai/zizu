import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import test from 'node:test'

function fakeChild(pid = 24680) {
  const events = new EventEmitter()
  return {
    pid,
    exitCode: null,
    killCalls: 0,
    kill() {
      this.killCalls += 1
      return true
    },
    once: events.once.bind(events),
    exit(code = 0) {
      this.exitCode = code
      events.emit('exit', code, null)
    },
  }
}

test('Windows fixture cleanup terminates the spawned PID tree and waits for its exit', async () => {
  const { stopSpawnedProcess } = await import('./localProcess.mjs')
  const child = fakeChild()
  const taskkillCalls = []
  let resolved = false

  const stopping = stopSpawnedProcess(child, {
    platform: 'win32',
    taskkill: async (arguments_) => { taskkillCalls.push(arguments_) },
  }).then(() => { resolved = true })

  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual(taskkillCalls, [['/PID', '24680', '/T', '/F']])
  assert.equal(child.killCalls, 0)
  assert.equal(resolved, false)
  child.exit()
  await stopping
  assert.equal(resolved, true)
})

test('non-Windows fixture cleanup retains direct child termination and waits for exit', async () => {
  const { stopSpawnedProcess } = await import('./localProcess.mjs')
  const child = fakeChild()
  let resolved = false

  const stopping = stopSpawnedProcess(child, { platform: 'linux' }).then(() => { resolved = true })

  assert.equal(child.killCalls, 1)
  assert.equal(resolved, false)
  child.exit()
  await stopping
  assert.equal(resolved, true)
})
