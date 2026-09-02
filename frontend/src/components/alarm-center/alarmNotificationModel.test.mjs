import assert from 'node:assert/strict'
import test from 'node:test'

test('delivery states have plain Chinese labels', async () => {
  const { describeDeliveryStatus } = await import('./alarmNotificationModel.ts')

  assert.equal(describeDeliveryStatus('pending'), '待发送')
  assert.equal(describeDeliveryStatus('retry_wait'), '等待重试')
  assert.equal(describeDeliveryStatus('delivered'), '已送达')
  assert.equal(describeDeliveryStatus('failed'), '发送失败')
  assert.equal(describeDeliveryStatus('cancelled'), '已取消')
})

test('only failed deliveries with an existing config can retry', async () => {
  const { canRetryDelivery } = await import('./alarmNotificationModel.ts')

  assert.equal(canRetryDelivery({ status: 'failed', configuration_exists: true }), true)
  assert.equal(canRetryDelivery({ status: 'failed', configuration_exists: false }), false)
  assert.equal(canRetryDelivery({ status: 'retry_wait', configuration_exists: true }), false)
})

test('transition types and stable errors are readable', async () => {
  const { describeDeliveryEvent, describeDeliveryError } = await import('./alarmNotificationModel.ts')

  assert.equal(describeDeliveryEvent('ALARM_ACTIVATED'), '告警发生')
  assert.equal(describeDeliveryEvent('ALARM_RECOVERED'), '告警恢复')
  assert.equal(describeDeliveryError('HTTP_NOTIFICATION_DELIVERY_TIMEOUT'), '目标服务响应超时')
})
