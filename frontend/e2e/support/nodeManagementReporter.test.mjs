import assert from 'node:assert/strict'
import test from 'node:test'

import { redactSecrets, summarizeAcceptanceRun } from './nodeManagementReporter.mjs'

test('summarizes pass, failure, duration and failure artifact paths', () => {
  const summary = summarizeAcceptanceRun({
    startedAt: 1_000,
    endedAt: 4_250,
    results: [
      { title: '节点 CRUD', status: 'passed', durationMs: 1_000, artifacts: [] },
      {
        title: 'L0 实时',
        status: 'failed',
        durationMs: 2_250,
        error: 'value did not arrive',
        artifacts: ['test-results/l0/screenshot.png', 'test-results/l0/trace.zip'],
      },
    ],
  })

  assert.deepEqual(summary.counts, { passed: 1, failed: 1, skipped: 0, total: 2 })
  assert.equal(summary.durationMs, 3_250)
  assert.deepEqual(summary.failures, [
    {
      title: 'L0 实时',
      error: 'value did not arrive',
      artifacts: ['test-results/l0/screenshot.png', 'test-results/l0/trace.zip'],
    },
  ])
})

test('redacts every configured secret from nested report values', () => {
  const redacted = redactSecrets(
    {
      error: 'login operator/private-password failed',
      nested: ['private-password', { actor: 'operator' }],
    },
    ['operator', 'private-password'],
  )

  assert.equal(JSON.stringify(redacted).includes('operator'), false)
  assert.equal(JSON.stringify(redacted).includes('private-password'), false)
  assert.match(redacted.error, /\[REDACTED\]/)
})
