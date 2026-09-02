import assert from 'node:assert/strict'
import test from 'node:test'

const enabledTestedDraft = {
  id: 'config-1',
  name: '值班群',
  description: null,
  method: 'POST',
  url: '',
  url_display: 'https://receiver.invalid/***',
  query_params: [],
  headers: [{ key: 'Authorization', value: '', sensitive: true, configured: true }],
  content_type: 'application/json',
  body_template: '{"type":{{event.type}}}',
  timeout_seconds: 5,
  current_digest: 'current',
  tested_digest: 'current',
  tested_at: '2026-09-02T10:00:00Z',
  last_test_status: { delivered: true, http_status: 204 },
  enabled: true,
}

test('material edits disable and require a fresh test', async () => {
  const { applyHttpNotificationEdit } = await import('./alarmHttpNotificationModel.ts')
  const next = applyHttpNotificationEdit(enabledTestedDraft, { timeout_seconds: 6 })

  assert.equal(next.enabled, false)
  assert.equal(next.tested_digest, null)
  assert.equal(next.tested_at, null)
  assert.equal(next.last_test_status, null)
})

test('descriptive edits preserve a valid test', async () => {
  const { applyHttpNotificationEdit } = await import('./alarmHttpNotificationModel.ts')
  const next = applyHttpNotificationEdit(enabledTestedDraft, { description: '主通道' })

  assert.equal(next.enabled, true)
  assert.equal(next.tested_digest, 'current')
})

test('stable backend errors have actionable Chinese copy', async () => {
  const { describeHttpNotificationError } = await import('./alarmHttpNotificationModel.ts')

  assert.equal(
    describeHttpNotificationError('HTTP_NOTIFICATION_TEST_STALE'),
    '请求内容已修改，请重新发送测试，成功后再启用。',
  )
})

test('masked preview never restores configured sensitive values', async () => {
  const { buildMaskedPreview } = await import('./alarmHttpNotificationModel.ts')
  const preview = buildMaskedPreview(enabledTestedDraft)

  assert.match(preview, /Authorization/)
  assert.match(preview, /\*\*\*/)
  assert.doesNotMatch(preview, /Bearer/)
  assert.doesNotMatch(preview, /secret-token/)
})
