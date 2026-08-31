import assert from 'node:assert/strict'
import test from 'node:test'

import { fixtureNames } from './e2eFixture.ts'


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
  })
})
