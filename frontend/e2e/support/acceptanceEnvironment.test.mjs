import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildAcceptanceEnvironment,
  buildTemporaryResourceName,
  printableAcceptanceSummary,
} from './acceptanceEnvironment.mjs'

const completeEnvironment = {
  ZIZU_E2E_BASE_URL: 'http://e606.hlszh.com:9000/',
  ZIZU_E2E_USERNAME: 'operator',
  ZIZU_E2E_PASSWORD: 'private-password',
  ZIZU_E2E_ALLOW_LIVE_WRITES: '1',
  ZIZU_E2E_WRITE_ROOT: 'E2E验证',
  ZIZU_E2E_RUN_ID: '20260830T120000Z',
}

test('requires the live site, credentials, explicit write acknowledgement and exact E2E root', () => {
  for (const key of [
    'ZIZU_E2E_BASE_URL',
    'ZIZU_E2E_USERNAME',
    'ZIZU_E2E_PASSWORD',
    'ZIZU_E2E_ALLOW_LIVE_WRITES',
    'ZIZU_E2E_WRITE_ROOT',
  ]) {
    assert.throws(
      () => buildAcceptanceEnvironment({ ...completeEnvironment, [key]: '' }),
      new RegExp(key),
    )
  }

  assert.throws(
    () => buildAcceptanceEnvironment({ ...completeEnvironment, ZIZU_E2E_ALLOW_LIVE_WRITES: 'yes' }),
    /ZIZU_E2E_ALLOW_LIVE_WRITES/,
  )
  assert.throws(
    () => buildAcceptanceEnvironment({ ...completeEnvironment, ZIZU_E2E_WRITE_ROOT: '储能' }),
    /E2E验证/,
  )
})

test('normalizes the URL and only creates namespaced temporary resource names', () => {
  const environment = buildAcceptanceEnvironment(completeEnvironment)
  assert.equal(environment.baseUrl, 'http://e606.hlszh.com:9000')
  assert.equal(
    buildTemporaryResourceName(environment, 'PCS'),
    'E2E验证-PCS-20260830T120000Z',
  )
  assert.throws(
    () => buildTemporaryResourceName(environment, '../PCS'),
    /资源标签/,
  )
})

test('printable summary never contains credentials', () => {
  const environment = buildAcceptanceEnvironment(completeEnvironment)
  const serialized = JSON.stringify(printableAcceptanceSummary(environment))

  assert.match(serialized, /e606\.hlszh\.com/)
  assert.doesNotMatch(serialized, /operator/)
  assert.doesNotMatch(serialized, /private-password/)
})
