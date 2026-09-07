import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

async function sourceModule(file) {
  const built = await build({ entryPoints: [fileURLToPath(new URL(file, import.meta.url))], bundle: true, write: false, format: 'esm', platform: 'browser' })
  return import(`data:text/javascript;base64,${Buffer.from(built.outputFiles[0].text).toString('base64')}`)
}

test('node directory HTTP failure is never an empty site, but a genuine empty list is valid', async (t) => {
  const { fetchNodes } = await sourceModule('./client.ts')
  for (const status of [401, 503]) {
    t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ detail: 'directory unavailable' }), { status }))
    await assert.rejects(fetchNodes, /directory unavailable/)
    t.mock.restoreAll()
  }
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ nodes: [] })))
  assert.deepEqual(await fetchNodes(), [])
  t.mock.restoreAll()
  t.mock.method(globalThis, 'fetch', async () => new Response('{}'))
  await assert.rejects(fetchNodes, /响应不完整/)
})

test('server close immediately reports stream loss; intentional unsubscribe does not', async (t) => {
  const { connectCommittedFrameStream } = await sourceModule('./committedFrameStream.ts')
  const savedWindow = globalThis.window
  const savedWebSocket = globalThis.WebSocket
  let created
  let signalCreated
  const ready = new Promise((resolve) => { signalCreated = resolve })
  const timers = []
  globalThis.window = { location: { protocol: 'http:', host: '127.0.0.1' }, setTimeout: (fn, delay) => { timers.push([fn, delay]); return 1 }, clearTimeout: () => {} }
  globalThis.WebSocket = class {
    constructor() { created = this; signalCreated() }
    close() { this.onclose?.({ code: 1000 }) }
    send() {}
  }
  t.after(() => {
    globalThis.window = savedWindow
    globalThis.WebSocket = savedWebSocket
  })
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ ticket: 'isolated-test' })))
  const failures = []
  const stop = connectCommittedFrameStream({ nodeId: 'node-a', cursor: 'frame-1', onDelta: () => {}, onResnapshotRequired: () => {}, onError: (error) => failures.push(error.message) })
  await ready
  created.onclose({ code: 1000 })
  assert.deepEqual(failures, ['实时数据连接已断开，正在重连'])
  assert.equal(timers.length, 1)
  stop()
  created.onclose({ code: 1000 })
  assert.equal(failures.length, 1)
})
