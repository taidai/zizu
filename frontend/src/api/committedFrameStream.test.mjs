import assert from 'node:assert/strict'
import test from 'node:test'

const stream = await import('./committedFrameRecovery.ts')

test('a transient snapshot failure retries and returns the first successful snapshot', async () => {
  let attempts = 0
  const delays = []
  const expected = { type: 'frame_snapshot', node_id: 'node-a', frame_sequence: 12 }

  const result = await stream.retryCommittedFrameSnapshot(
    async () => {
      attempts += 1
      if (attempts === 1) throw new Error('temporary 503')
      return expected
    },
    () => true,
    async (delay) => { delays.push(delay) },
  )

  assert.strictEqual(result, expected)
  assert.equal(attempts, 2)
  assert.deepEqual(delays, [1000])
})

test('snapshot recovery stops after the selected node is no longer active', async () => {
  let active = true
  let attempts = 0

  const result = await stream.retryCommittedFrameSnapshot(
    async () => {
      attempts += 1
      throw new Error('temporary 503')
    },
    () => active,
    async () => { active = false },
  )

  assert.equal(result, null)
  assert.equal(attempts, 1)
})
