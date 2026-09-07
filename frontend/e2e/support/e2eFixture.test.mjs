import assert from 'node:assert/strict'
import path from 'node:path'
import test from 'node:test'

import {
  encodeFixtureScalar,
  fixtureNames,
  fixtureTimeoutMs,
  resolveFixtureScript,
} from './e2eFixture.ts'


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

test('explicit local fixture script is available only for a loopback acceptance site', () => {
  const repositoryRoot = path.resolve('C:/workspace/zizu')
  const localScript = path.resolve('C:/private/local_fixture.py')

  assert.equal(
    resolveFixtureScript(
      repositoryRoot,
      { baseUrl: 'http://127.0.0.1:19112' },
      { ZIZU_E2E_LOCAL_FIXTURE_SCRIPT: localScript },
    ),
    localScript,
  )
})

test('fixture keeps the production script unless the local override is explicit', () => {
  const repositoryRoot = path.resolve('C:/workspace/zizu')

  assert.equal(
    resolveFixtureScript(repositoryRoot, { baseUrl: 'https://site.example' }, {}),
    path.join(repositoryRoot, 'backend', 'scripts', 'node_management_e2e_fixture.py'),
  )
})

test('local fixture override refuses non-loopback and ambiguous script paths', () => {
  const repositoryRoot = path.resolve('C:/workspace/zizu')
  const localScript = path.resolve('C:/private/local_fixture.py')

  for (const baseUrl of [
    'https://site.example',
    'http://localhost:19112',
    'https://127.0.0.1:19112',
  ]) {
    assert.throws(
      () => resolveFixtureScript(
        repositoryRoot,
        { baseUrl },
        { ZIZU_E2E_LOCAL_FIXTURE_SCRIPT: localScript },
      ),
      /HTTP loopback/,
      baseUrl,
    )
  }
  assert.throws(
    () => resolveFixtureScript(
      repositoryRoot,
      { baseUrl: 'http://127.0.0.1:19112' },
      { ZIZU_E2E_LOCAL_FIXTURE_SCRIPT: 'local_fixture.py' },
    ),
    /absolute Python script path/,
  )
})

test('local fixture override refuses every SSH or sudo option', () => {
  const repositoryRoot = path.resolve('C:/workspace/zizu')
  const localScript = path.resolve('C:/private/local_fixture.py')

  for (const key of [
    'ZIZU_E2E_SSH_HOST',
    'ZIZU_E2E_SSH_PORT',
    'ZIZU_E2E_SSH_USER',
    'ZIZU_E2E_SSH_PASSWORD',
    'ZIZU_E2E_SUDO_PASSWORD',
  ]) {
    assert.throws(
      () => resolveFixtureScript(
        repositoryRoot,
        { baseUrl: 'http://127.0.0.1:19112' },
        { ZIZU_E2E_LOCAL_FIXTURE_SCRIPT: localScript, [key]: 'set' },
      ),
      /SSH|sudo/,
      key,
    )
  }
})
