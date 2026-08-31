import assert from 'node:assert/strict'
import test from 'node:test'

test('physical point catalog uses legal 200-row pages and follows pagination', async () => {
  const model = await import('./rawPointHistoryModel.ts')
  const calls = []
  const tags = await model.loadPhysicalNumericTags('node-a', async (...args) => {
    calls.push(args)
    const page = args[1]
    return {
      tags: page === 1
        ? [{ id: 'float', data_type: 'FLOAT' }, { id: 'bool', data_type: 'BOOL' }]
        : [{ id: 'int', data_type: 'INT' }],
      total_pages: 2,
    }
  })

  assert.deepEqual(calls.map((args) => [args[0], args[1], args[2], args[5], args[10]]), [
    ['node-a', 1, 200, 'PHYSICAL', true],
    ['node-a', 2, 200, 'PHYSICAL', true],
  ])
  assert.deepEqual(tags.map((tag) => tag.id), ['float', 'int'])
})

test('history stays idle until one point is selected then makes exactly one request', async () => {
  const model = await import('./rawPointHistoryModel.ts')
  const calls = []
  const fetchHistory = async (...args) => {
    calls.push(args)
    return { points: [{ ts: '2026-08-28T00:00:00Z', raw_value: 1, eng_value: 1 }] }
  }

  assert.deepEqual(await model.loadSelectedRawPointHistory(null, '1h', fetchHistory), [])
  assert.equal(calls.length, 0)
  assert.equal((await model.loadSelectedRawPointHistory('tag-a', '24h', fetchHistory)).length, 1)
  assert.deepEqual(calls, [['tag-a', '24h']])
})

test('a delayed response from the previous node cannot commit after node switch', async () => {
  const model = await import('./rawPointHistoryModel.ts')
  let resolveA
  const delayedA = new Promise((resolve) => { resolveA = resolve })
  const commits = []
  let currentNodeId = 'node-a'
  let generation = 1
  const requestA = delayedA.then((value) => {
    if (model.requestResultIsCurrent({ requestGeneration: 1, currentGeneration: generation, expectedNodeId: 'node-a', currentNodeId, resultNodeId: value.node_id })) commits.push(value.node_id)
  })

  currentNodeId = 'node-b'
  generation = 2
  if (model.requestResultIsCurrent({ requestGeneration: 2, currentGeneration: generation, expectedNodeId: 'node-b', currentNodeId, resultNodeId: 'node-b' })) commits.push('node-b')
  resolveA({ node_id: 'node-a' })
  await requestA

  assert.deepEqual(commits, ['node-b'])
})
