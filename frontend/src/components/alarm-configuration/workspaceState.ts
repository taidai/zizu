import type { AlarmConfigurationPlan } from '../../api/client'

export const PLAN_CONTEXT_KEY = 'zizu.alarm-configuration.plan'
export const APPLY_CONTEXT_KEY = 'zizu.alarm-configuration.apply'

export interface SavedPlanContext { id: string; revision: string; digest: string; baseVersion: number }
export interface SavedApplyContext { planId: string; digest: string; key: string }
export interface WorkspaceContext { plan: SavedPlanContext | null; apply: SavedApplyContext | null }

function read<T>(storage: Storage, key: string): T | null {
  try { return JSON.parse(storage.getItem(key) || 'null') as T | null } catch { return null }
}

export function readWorkspaceContext(storage: Storage = sessionStorage): WorkspaceContext {
  const plan = read<SavedPlanContext>(storage, PLAN_CONTEXT_KEY)
  const apply = read<SavedApplyContext>(storage, APPLY_CONTEXT_KEY)
  return {
    plan: plan && typeof plan.id === 'string' && typeof plan.revision === 'string' && typeof plan.digest === 'string' && Number.isFinite(plan.baseVersion) ? plan : null,
    apply: apply && typeof apply.planId === 'string' && typeof apply.digest === 'string' && typeof apply.key === 'string' ? apply : null,
  }
}

export function savePlanContext(plan: AlarmConfigurationPlan, storage: Storage = sessionStorage): void {
  storage.setItem(PLAN_CONTEXT_KEY, JSON.stringify({ id: plan.id, revision: `${plan.rule_set_revision.rule_set_id}:${plan.rule_set_revision.revision}`, digest: plan.digest, baseVersion: plan.base_site_configuration_version } satisfies SavedPlanContext))
  storage.removeItem(APPLY_CONTEXT_KEY)
}

export function saveApplyContext(plan: AlarmConfigurationPlan, key: string, storage: Storage = sessionStorage): void {
  storage.setItem(APPLY_CONTEXT_KEY, JSON.stringify({ planId: plan.id, digest: plan.digest, key } satisfies SavedApplyContext))
}

export function clearWorkspaceContext(storage: Storage = sessionStorage): void {
  storage.removeItem(PLAN_CONTEXT_KEY)
  storage.removeItem(APPLY_CONTEXT_KEY)
}

export function clearApplyContext(storage: Storage = sessionStorage): void {
  storage.removeItem(APPLY_CONTEXT_KEY)
}

export function canReplaySavedApply(context: WorkspaceContext, plan: AlarmConfigurationPlan): boolean {
  return context.plan?.id === plan.id && context.plan.digest === plan.digest && context.apply?.planId === plan.id && context.apply.digest === plan.digest
}
