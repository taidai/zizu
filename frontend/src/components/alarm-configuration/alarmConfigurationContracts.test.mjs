import assert from 'node:assert/strict'
import test from 'node:test'

test('2xx apply response with an unreadable JSON body is result-unknown', async () => {
  const parseFailure = new Error('truncated response body')

  await assert.rejects(
    async () => {
      const contracts = await import('./alarmConfigurationContracts.ts')
      await contracts.readAlarmConfigurationApplyResult({
        ok: true,
        status: 200,
        json: async () => { throw parseFailure },
      })
    },
    (error) => error?.name === 'AlarmConfigurationResultUnknownError' && error.cause === parseFailure,
  )
})

test('only an explicit non-retry domain code clears apply replay evidence', async () => {
  const contracts = await import('./alarmConfigurationContracts.ts')

  assert.equal(contracts.isDefinitiveAlarmApplyCode(null), false)
  assert.equal(contracts.isDefinitiveAlarmApplyCode('AUDIT_UNAVAILABLE'), false)
  assert.equal(contracts.isDefinitiveAlarmApplyCode('ALARM_PLAN_BLOCKED'), true)
})

test('alarm condition values render booleans in Chinese and preserve negative numbers', async () => {
  const contracts = await import('./alarmConfigurationContracts.ts')

  assert.equal(typeof contracts.formatAlarmConditionValue, 'function')
  assert.equal(contracts.formatAlarmConditionValue(true), '是')
  assert.equal(contracts.formatAlarmConditionValue(false), '否')
  assert.equal(contracts.formatAlarmConditionValue(-12.5), '-12.5')
  assert.equal(contracts.formatAlarmConditionValue('运行'), '运行')
})

test('new alarm rules default to no HTTP notification', async () => {
  const { defaultAlarmDraft } = await import('../alarm-center/alarmCenterModel.ts')

  assert.equal(defaultAlarmDraft('NUMBER').http_notification_config_id, null)
})

test('editing an alarm rule preserves its HTTP notification binding', async () => {
  const { defaultAlarmDraft, prepareAlarmRuleEdit } = await import('../alarm-center/alarmCenterModel.ts')
  const saved = {
    ...defaultAlarmDraft('NUMBER'),
    http_notification_config_id: '00000000-0000-0000-0000-000000000201',
  }

  const prepared = prepareAlarmRuleEdit([saved], ['FLOAT'])

  assert.equal(prepared.ready, true)
  assert.equal(prepared.rules[0].http_notification_config_id, saved.http_notification_config_id)
})
