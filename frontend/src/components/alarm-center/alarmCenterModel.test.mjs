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

test('alarm trial stays blocked until one entity and a valid typed value are ready', async () => {
  const { prepareAlarmTrialInput } = await import('./alarmCenterModel.ts')

  assert.deepEqual(prepareAlarmTrialInput('NUMBER', null, ''), {
    ready: false,
    message: '请先在第 1 步勾选一个实体。',
  })
  assert.deepEqual(prepareAlarmTrialInput('NUMBER', 'INT', ''), {
    ready: false,
    message: '请输入有效的数值再试算。',
  })
  assert.deepEqual(prepareAlarmTrialInput('NUMBER', 'INT', '0'), {
    ready: true,
    value: 0,
    message: '可以试算。',
  })
})

test('boolean alarm trials use real booleans instead of free-text state values', async () => {
  const { defaultAlarmDraft, prepareAlarmTrialInput } = await import('./alarmCenterModel.ts')

  const draft = defaultAlarmDraft('STATE', 'BOOL')
  assert.equal(draft.trigger.value, true)
  assert.equal(draft.recovery.value, false)
  assert.deepEqual(prepareAlarmTrialInput('STATE', 'BOOL', 'false'), {
    ready: true,
    value: false,
    message: '可以试算。',
  })
})

test('editing a legacy text-state rule for a boolean entity restores typed conditions', async () => {
  const { defaultAlarmDraft, normalizeAlarmRuleForEntity } = await import('./alarmCenterModel.ts')
  const legacyRule = {
    ...defaultAlarmDraft('STATE'),
    name: '15V 电源故障',
    severity: 'MAJOR',
  }

  const normalized = normalizeAlarmRuleForEntity(legacyRule, 'BOOL')

  assert.equal(normalized.name, '15V 电源故障')
  assert.equal(normalized.severity, 'MAJOR')
  assert.deepEqual(normalized.trigger, { operator: 'eq', value: true })
  assert.deepEqual(normalized.recovery, { operator: 'eq', value: false })
})

test('boolean rule normalization preserves each condition that is already typed', async () => {
  const { defaultAlarmDraft, normalizeAlarmRuleForEntity } = await import('./alarmCenterModel.ts')
  const halfMigrated = {
    ...defaultAlarmDraft('STATE'),
    trigger: { operator: 'eq', value: false },
  }

  const normalized = normalizeAlarmRuleForEntity(halfMigrated, 'BOOLEAN')

  assert.deepEqual(normalized.trigger, { operator: 'eq', value: false })
  assert.deepEqual(normalized.recovery, { operator: 'eq', value: false })
})

test('boolean rule normalization preserves numeric legacy conditions', async () => {
  const { defaultAlarmDraft, normalizeAlarmRuleForEntity } = await import('./alarmCenterModel.ts')
  const numericLegacyRule = {
    ...defaultAlarmDraft('STATE'),
    trigger: { operator: 'eq', value: 0 },
    recovery: { operator: 'eq', value: 1 },
  }

  const normalized = normalizeAlarmRuleForEntity(numericLegacyRule, 'BOOL')

  assert.deepEqual(normalized.trigger, { operator: 'eq', value: 0 })
  assert.deepEqual(normalized.recovery, { operator: 'eq', value: 1 })
})

test('rule editing accepts one compatible type family and rejects mixed bindings', async () => {
  const { defaultAlarmDraft, prepareAlarmRuleEdit } = await import('./alarmCenterModel.ts')
  const legacyRule = defaultAlarmDraft('STATE')

  assert.deepEqual(prepareAlarmRuleEdit([legacyRule], ['BOOL', 'BOOLEAN']), {
    ready: true,
    booleanEntity: true,
    rules: [{
      ...legacyRule,
      trigger: { operator: 'eq', value: true },
      recovery: { operator: 'eq', value: false },
    }],
  })
  assert.deepEqual(prepareAlarmRuleEdit([legacyRule], ['BOOL', 'STRING']), {
    ready: false,
    message: '该规则组混合绑定了不同类型的实体，请拆分规则组后再编辑。',
  })
  assert.deepEqual(prepareAlarmRuleEdit([legacyRule], []), {
    ready: false,
    message: '找不到规则组绑定实体的类型信息，请刷新后重试。',
  })
})

test('alarm trial summary shows the tested entity, value, conditions, and outcome', async () => {
  const { defaultAlarmDraft, describeAlarmTrialResult } = await import('./alarmCenterModel.ts')
  const rule = defaultAlarmDraft('STATE', 'BOOL')

  assert.equal(
    describeAlarmTrialResult(
      {
        entity_instance_id: 'entity-1',
        trigger_matches: false,
        recovery_matches: true,
        description: '状态告警：当前值未命中触发条件，命中恢复条件。',
      },
      rule,
      false,
      { displayName: 'E2E验证 / 15V电源故障', unit: null },
    ),
    'E2E验证 / 15V电源故障：试算值 false；结果：会恢复。触发条件 = true（未命中），恢复条件 = false（命中）。',
  )
})

test('only recovered unarchived alarm events expose archive action', async () => {
  const { canArchiveAlarmEvent } = await import('./alarmCenterModel.ts')

  assert.equal(canArchiveAlarmEvent({ state: 'recovered', archived_at: null }), true)
  assert.equal(canArchiveAlarmEvent({ state: 'active_acknowledged', archived_at: null }), false)
  assert.equal(canArchiveAlarmEvent({ state: 'recovered', archived_at: '2026-09-04T00:00:00Z' }), false)
})

test('only a disabled alarm rule group exposes delete action', async () => {
  const { canDeleteAlarmRuleGroup } = await import('./alarmCenterModel.ts')

  assert.equal(canDeleteAlarmRuleGroup({ enabled_entity_instance_ids: [] }), true)
  assert.equal(canDeleteAlarmRuleGroup({ enabled_entity_instance_ids: ['entity-1'] }), false)
})
