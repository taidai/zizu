import assert from 'node:assert/strict'
import test from 'node:test'

import { encodeFixtureScalar, fixtureNames, fixtureTimeoutMs } from './e2eFixture.ts'


test('fixture names use the one acceptance run id supplied by the suite', () => {
  const environment = {
    writeRoot: 'E2E验证',
    runId: 'fixed-run-id',
  }

  assert.deepEqual(fixtureNames(environment), {
    root: 'E2E验证',
    platformNode: 'E2E验证-设备-fixed-run-id',
    neuronNode: 'zizu_e2e_fixed_run_id',
    neuronGroup: 'e2e_data',
    neuronTag: 'e2e_active_power',
    bitTag: 'e2e_fault_bit',
  })
})

test('fixture scalar encoding preserves numbers, booleans, and strings', () => {
  assert.deepEqual(
    [0, 1, 2, false, true, '0', '1'].map(encodeFixtureScalar),
    ['0', '1', '2', 'false', 'true', '"0"', '"1"'],
  )
})

test('cleanup gets enough time for API retirement and private Neuron cleanup', () => {
  assert.equal(fixtureTimeoutMs('cleanup'), 90_000)
  assert.equal(fixtureTimeoutMs('publish'), 30_000)
})
