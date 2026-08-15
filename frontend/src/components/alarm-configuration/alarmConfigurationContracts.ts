export class AlarmConfigurationResultUnknownError extends Error {
  readonly cause: unknown

  constructor(cause: unknown) {
    super('Alarm configuration request result is unknown')
    this.name = 'AlarmConfigurationResultUnknownError'
    this.cause = cause
  }
}

export type AlarmConditionValue = number | string | boolean

export function formatAlarmConditionValue(value: AlarmConditionValue): string {
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

interface SuccessfulJsonResponse {
  readonly ok: boolean
  readonly status: number
  json(): Promise<unknown>
}

export async function readAlarmConfigurationApplyResult<T>(response: SuccessfulJsonResponse): Promise<T> {
  try {
    return await response.json() as T
  } catch (cause) {
    throw new AlarmConfigurationResultUnknownError(cause)
  }
}

const DEFINITIVE_APPLY_CODES = new Set([
  'ALARM_CONFIGURATION_REQUEST_INVALID',
  'ALARM_PLAN_NOT_FOUND',
  'ALARM_PLAN_STALE',
  'ALARM_PLAN_DIGEST_MISMATCH',
  'ALARM_PLAN_BLOCKED',
  'IDEMPOTENCY_KEY_REUSED',
])

export function isDefinitiveAlarmApplyCode(code: string | null): boolean {
  return code !== null && DEFINITIVE_APPLY_CODES.has(code)
}
