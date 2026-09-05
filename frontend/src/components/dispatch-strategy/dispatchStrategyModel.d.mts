export type DispatchAction = 'CHARGE' | 'DISCHARGE' | 'HOLD'

export interface DispatchWindow {
  key: string
  start: string
  end: string
  action: DispatchAction
  target: number | string
  socMin: number | string
  socMax: number | string
}

export interface StrategyEntityLike {
  id: string
  data_type: string
  unit: string | null
  freshness_seconds: number
}

export function splitCrossMidnight(window: DispatchWindow): DispatchWindow[]
export function normalizeDispatchWindows(windows: readonly DispatchWindow[]): DispatchWindow[]
export function validateDispatchWindows(windows: readonly DispatchWindow[], safeTarget: unknown): {
  valid: boolean
  overlapKeys: string[]
  message: string
  rows: DispatchWindow[]
}
export function makeStrategyBinding(
  entity: StrategyEntityLike,
  direction: 'INPUT' | 'OUTPUT',
  bindingKey: string,
  ordinal: number,
): {
  direction: 'INPUT' | 'OUTPUT'
  binding_key: string
  ordinal: number
  entity_instance_id: string
  expected_data_type: string
  unit: string | null
  freshness_seconds: number
}
export function isDispatchSocEntity(entity: {
  confirmed: boolean
  direction: string
  definition_id: string
  data_type: string
  unit: string | null
} | null | undefined): boolean
export function isDispatchPowerTargetEntity(entity: {
  confirmed: boolean
  direction: string
  data_type: string
  unit: string | null
} | null | undefined): boolean
export function buildTwoChargeTwoDischargeJdm(
  windows: readonly DispatchWindow[],
  safeTarget: unknown,
): Record<string, any>
export function readTwoChargeTwoDischargeJdm(graph: Record<string, any>): {
  rows: DispatchWindow[]
  safeTarget: number
} | null
export function isJdmGraphUnchanged(left: Record<string, any>, right: Record<string, any>): boolean
export function describeDispatchStrategyError(reason: any): string
export function projectStrategyStatus(strategy: any): {
  draftRevision: number | null
  publishedRevision: number | null
  lifecycleLabel: string
  enableLabel: string
  healthLabel: string
  healthDetail: string
}
