import test from 'node:test'
import assert from 'node:assert/strict'
import { NeuronImportApiError, NeuronImportResultUnknownError, readNeuronImportResult } from './neuronImportResult.ts'

test('Neuron import distinguishes explicit 409 rejection from server uncertainty', async () => {
  for (const status of [409, 503]) {
    await assert.rejects(readNeuronImportResult(new Response(JSON.stringify({ detail: { code: 'REVISION_CHANGED', message: '需要重查' } }), { status })), (error) => error instanceof NeuronImportApiError && error.status === status && error.code === 'REVISION_CHANGED')
  }
})
test('Neuron import unreadable or malformed success is result unknown, never false failure', async () => {
  for (const body of ['{', '{}', '{"status":"ok","configuration_revision":3,"counts":null}']) {
    await assert.rejects(readNeuronImportResult(new Response(body, { status: 200 })), NeuronImportResultUnknownError)
  }
})
test('Neuron import preserves a complete successful receipt including zero counts', async () => {
  const receipt = { status: 'ok', configuration_revision: 3, counts: { create: 0, update: 2, unchanged: 4, conflict: 0 } }
  assert.deepEqual(await readNeuronImportResult(new Response(JSON.stringify(receipt))), receipt)
})
