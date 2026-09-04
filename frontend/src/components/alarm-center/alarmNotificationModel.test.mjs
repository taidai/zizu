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

test('only terminal delivery records can be selected for deletion', async () => {
  const { canDeleteDelivery, deletableDeliveryIds } = await import('./alarmNotificationModel.ts')
  const deliveries = [
    { id: 'pending', status: 'pending' },
    { id: 'retry', status: 'retry_wait' },
    { id: 'delivered', status: 'delivered' },
    { id: 'failed', status: 'failed' },
    { id: 'cancelled', status: 'cancelled' },
  ]

  assert.equal(canDeleteDelivery(deliveries[0]), false)
  assert.equal(canDeleteDelivery(deliveries[1]), false)
  assert.deepEqual(deletableDeliveryIds(deliveries), ['delivered', 'failed', 'cancelled'])
})

test('an empty last page falls back to the last page that still exists', async () => {
  const { validDeliveryPage } = await import('./alarmNotificationModel.ts')

  assert.equal(validDeliveryPage(2, 1), 1)
  assert.equal(validDeliveryPage(2, 3), 2)
})
