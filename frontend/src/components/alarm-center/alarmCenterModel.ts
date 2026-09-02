import type { AlarmRule, AlarmSeverity } from '../../api/client'

export interface FaultCodeRow {
  code: string
  name: string
  severity: AlarmSeverity
}

export interface AlarmPreviewEntity {
  displayName: string
  unit: string | null
}

export type AlarmDraftDataType = 'NUMBER' | 'STATE' | 'CODE_SET'

export type AlarmTrialInput =
  | { ready: false; message: string }
  | { ready: true; value: number | boolean | string | string[]; message: string }

const SEVERITIES = new Set<AlarmSeverity>(['CRITICAL', 'MAJOR', 'WARNING', 'INFO'])
const OPERATOR_LABEL = {
  eq: '=', ne: '≠', gt: '>', gte: '≥', lt: '<', lte: '≤',
  contains: '包含', not_contains: '不包含',
} as const
const SEVERITY_LABEL: Record<AlarmSeverity, string> = {
  CRITICAL: '紧急', MAJOR: '重要', WARNING: '警告', INFO: '提示',
}

export function parseFaultCodePaste(text: string): FaultCodeRow[] {
  const rows: FaultCodeRow[] = []
  const seen = new Set<string>()
  for (const [index, rawLine] of text.split(/\r?\n/).entries()) {
    if (!rawLine.trim()) continue
    const [rawCode = '', rawName = '', rawSeverity = '', ...extra] = rawLine.split('\t')
    const line = index + 1
    const code = rawCode.trim()
    const name = rawName.trim()
    const severity = rawSeverity.trim().toUpperCase() as AlarmSeverity
    if (extra.length || !code || !name || !SEVERITIES.has(severity)) {
      throw new Error(`第 ${line} 行：请按“故障码、故障名称、等级”三列填写`)
    }
    if (seen.has(code)) throw new Error(`第 ${line} 行：故障码 ${code} 重复`)
    seen.add(code)
    rows.push({ code, name, severity })
  }
  if (!rows.length) throw new Error('请至少粘贴一行故障码')
  return rows
}

export function compileFaultCodeRules(rows: FaultCodeRow[]): AlarmRule[] {
  return rows.map((row) => ({
    id: `fault-${row.code.toLowerCase().replace(/[^a-z0-9_-]+/g, '-')}`,
    name: row.name,
    severity: row.severity,
    trigger: { operator: 'contains', value: row.code },
    trigger_duration_seconds: 0,
    recovery: { operator: 'not_contains', value: row.code },
    recovery_duration_seconds: 3,
    notification_throttle_seconds: 300,
    unit: null,
    fault_map_id: null,
  }))
}

export function prepareAlarmTrialInput(
  dataType: AlarmDraftDataType,
  entityDataType: string | null | undefined,
  rawValue: string,
): AlarmTrialInput {
  if (!entityDataType) return { ready: false, message: '请先在第 1 步勾选一个实体。' }
  const value = rawValue.trim()
  if (dataType === 'NUMBER') {
    const parsed = Number(value)
    return value && Number.isFinite(parsed)
      ? { ready: true, value: parsed, message: '可以试算。' }
      : { ready: false, message: '请输入有效的数值再试算。' }
  }
  if (dataType === 'CODE_SET') {
    const codes = value.split(/[,，\s]+/).filter(Boolean)
    return codes.length
      ? { ready: true, value: codes, message: '可以试算。' }
      : { ready: false, message: '请至少输入一个故障码再试算。' }
  }
  if (['BOOL', 'BOOLEAN'].includes(entityDataType.toUpperCase())) {
    if (['true', '1'].includes(value.toLowerCase())) return { ready: true, value: true, message: '可以试算。' }
    if (['false', '0'].includes(value.toLowerCase())) return { ready: true, value: false, message: '可以试算。' }
    return { ready: false, message: '布尔实体的试算值只能选择 true 或 false。' }
  }
  return value
    ? { ready: true, value, message: '可以试算。' }
    : { ready: false, message: '请输入状态值再试算。' }
}

export function defaultAlarmDraft(dataType: AlarmDraftDataType, entityDataType?: string | null): AlarmRule {
  if (dataType === 'STATE') {
    const booleanEntity = ['BOOL', 'BOOLEAN'].includes((entityDataType || '').toUpperCase())
    return {
      id: 'state-alarm', name: '状态告警', severity: 'WARNING',
      trigger: { operator: 'eq', value: booleanEntity ? true : '故障' }, trigger_duration_seconds: 0,
      recovery: { operator: booleanEntity ? 'eq' : 'ne', value: booleanEntity ? false : '故障' }, recovery_duration_seconds: 3,
      notification_throttle_seconds: 300, unit: null, fault_map_id: null,
    }
  }
  if (dataType === 'CODE_SET') {
    return {
      id: 'fault-code', name: '故障码', severity: 'WARNING',
      trigger: { operator: 'contains', value: 'E01' }, trigger_duration_seconds: 0,
      recovery: { operator: 'not_contains', value: 'E01' }, recovery_duration_seconds: 3,
      notification_throttle_seconds: 300, unit: null, fault_map_id: null,
    }
  }
  return {
    id: 'threshold-alarm', name: '阈值告警', severity: 'WARNING',
    trigger: { operator: 'gte', value: 0 }, trigger_duration_seconds: 3,
    recovery: { operator: 'lte', value: 0 }, recovery_duration_seconds: 3,
    notification_throttle_seconds: 300, unit: null, fault_map_id: null,
  }
}

export function describeAlarmDraft(draft: AlarmRule, entity: AlarmPreviewEntity): string {
  const unit = entity.unit ? ` ${entity.unit}` : ''
  const triggerDuration = draft.trigger_duration_seconds > 0 ? ` 持续 ${draft.trigger_duration_seconds} 秒` : ''
  const recoveryDuration = draft.recovery_duration_seconds > 0 ? ` 持续 ${draft.recovery_duration_seconds} 秒` : ''
  return `${entity.displayName} ${OPERATOR_LABEL[draft.trigger.operator]} ${String(draft.trigger.value)}${unit}${triggerDuration}，产生${SEVERITY_LABEL[draft.severity]}告警；${OPERATOR_LABEL[draft.recovery.operator]} ${String(draft.recovery.value)}${unit}${recoveryDuration}后恢复。`
}
