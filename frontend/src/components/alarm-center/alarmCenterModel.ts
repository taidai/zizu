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

export function defaultAlarmDraft(dataType: AlarmDraftDataType): AlarmRule {
  if (dataType === 'STATE') {
    return {
      id: 'state-alarm', name: '状态告警', severity: 'WARNING',
      trigger: { operator: 'eq', value: '故障' }, trigger_duration_seconds: 0,
      recovery: { operator: 'ne', value: '故障' }, recovery_duration_seconds: 3,
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
