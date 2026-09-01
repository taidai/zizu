import assert from 'node:assert/strict'
import test from 'node:test'

const projection = await import('./committedFrameProjection.ts')

const snapshot = (sequence = 10) => ({
  type: 'frame_snapshot',
  node_id: 'node-a',
  cursor: `cursor-${sequence}`,
  frame_sequence: sequence,
  frame_time: '2026-08-27T10:00:00Z',
  configuration_revision: 46,
  frame_status: 'FAILED',
  failure: { code: 'FRAME_PROCESSING_FAILED' },
  backlog_frames: 3,
  l0: [{ tag_id: 'tag-a', value: 1, frame_sequence: sequence }],
  l2: [{ entity_instance_id: 'entity-a', value: 2, frame_sequence: sequence }],
})

const delta = (sequence = 11) => ({
  type: 'frame_delta',
  cursor: `cursor-${sequence}`,
  frame_id: `frame-${sequence}`,
  frame_sequence: sequence,
  status: 'COMPLETE',
  frame_time: '2026-08-27T10:00:01Z',
  configuration_revision: 47,
  l0_changes: [{ tag_id: 'tag-a', value: 3 }],
  l2_changes: [{ entity_instance_id: 'entity-a', value: 4 }],
  failure: null,
})

test('one frame applies l0 and l2 atomically and rejects duplicates', () => {
  const initial = projection.replaceSnapshot(null, snapshot(10))
  const next = projection.applyFrameDelta(initial, delta(11))

  assert.equal(next.frameSequence, 11)
  assert.equal(next.l0.get('tag-a').value, 3)
  assert.equal(next.l0.get('tag-a').frame_sequence, 11)
  assert.equal(next.l2.get('entity-a').value, 4)
  assert.equal(next.l2.get('entity-a').frame_sequence, 11)
  assert.strictEqual(projection.applyFrameDelta(next, delta(11)), next)
})

test('snapshot replacement cannot retain values from the previous node', () => {
  const first = projection.replaceSnapshot(null, snapshot(10))
  const replacement = projection.replaceSnapshot(first, {
    ...snapshot(20),
    node_id: 'node-b',
    l0: [{ tag_id: 'tag-b', value: 8, frame_sequence: 20 }],
    l2: [],
  })

  assert.equal(replacement.nodeId, 'node-b')
  assert.equal(replacement.l0.has('tag-a'), false)
  assert.equal(replacement.l0.get('tag-b').value, 8)
  assert.equal(replacement.l2.size, 0)
})

test('snapshot replacement keeps data-frame diagnostics for the selected node', () => {
  const current = projection.replaceSnapshot(null, snapshot(10))

  assert.equal(current.status, 'FAILED')
  assert.deepEqual(current.failure, { code: 'FRAME_PROCESSING_FAILED' })
  assert.equal(current.backlogFrames, 3)
})

test('a missing global checkpoint fails closed', () => {
  const initial = projection.replaceSnapshot(null, snapshot(10))
  assert.throws(
    () => projection.applyFrameDelta(initial, delta(12)),
    /FRAME_SEQUENCE_GAP/,
  )
})
