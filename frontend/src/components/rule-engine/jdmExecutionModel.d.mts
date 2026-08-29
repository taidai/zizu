import type { JdmExecutionSummary } from '../../api/client'

export function isEditableJdmRuleType(ruleType: string): boolean

export function jdmExecutionLabel(
  execution: Pick<JdmExecutionSummary, 'status' | 'reason_code'> | null | undefined,
): string

export function latestJdmExecution<T extends { frame_sequence: number }>(
  executions: T[],
): T | null
