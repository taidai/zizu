import assert from 'node:assert/strict'
import test from 'node:test'

test('three-column fault paste compiles each code into an independent rule', async () => {
  const { compileFaultCodeRules, parseFaultCodePaste } = await import('./alarmCenterModel.ts')
  const rows = parseFaultCodePaste('E30\t压缩机故障\tMAJOR\nE42\t直流母线过压\tCRITICAL')
  const rules = compileFaultCodeRules(rows)

  assert.deepEqual(rows, [
    { code: 'E30', name: '压缩机故障', severity: 'MAJOR' },
    { code: 'E42', name: '直流母线过压', severity: 'CRITICAL' },
  ])
  assert.equal(rules.length, 2)
  assert.deepEqual(rules[0].trigger, { operator: 'contains', value: 'E30' })
  assert.deepEqual(rules[0].recovery, { operator: 'not_contains', value: 'E30' })
  assert.equal(rules[0].recovery_duration_seconds, 3)
})

test('fault paste reports the exact row containing a duplicate code', async () => {
  const { parseFaultCodePaste } = await import('./alarmCenterModel.ts')

  assert.throws(
    () => parseFaultCodePaste('E30\t压缩机故障\tMAJOR\nE30\t压缩机故障2\tWARNING'),
    /第 2 行：故障码 E30 重复/,
  )
})

test('typed drafts use safe defaults and produce a readable Chinese preview', async () => {
  const { defaultAlarmDraft, describeAlarmDraft } = await import('./alarmCenterModel.ts')
  const numeric = defaultAlarmDraft('NUMBER')
  const state = defaultAlarmDraft('STATE')

  assert.equal(numeric.trigger_duration_seconds, 3)
  assert.equal(numeric.recovery_duration_seconds, 3)
  assert.equal(state.trigger_duration_seconds, 0)
  assert.equal(state.recovery_duration_seconds, 3)
  assert.equal(
    describeAlarmDraft(
      {
        ...numeric,
        name: '功率越限',
        severity: 'MAJOR',
        trigger: { operator: 'gte', value: 100 },
        recovery: { operator: 'lte', value: 95 },
      },
      { displayName: 'PCS-01 有功功率', unit: 'kW' },
    ),
    'PCS-01 有功功率 ≥ 100 kW 持续 3 秒，产生重要告警；≤ 95 kW 持续 3 秒后恢复。',
  )
})
