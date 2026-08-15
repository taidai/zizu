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
