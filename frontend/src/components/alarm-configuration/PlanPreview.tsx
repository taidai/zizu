import { useMemo, useState } from 'react'
import type { AlarmConfigurationPlan, AlarmConfigurationPlanItem } from '../../api/client'

interface Props {
  plan: AlarmConfigurationPlan
  entityNames: Map<string, string>
  applying: boolean
  stale: boolean
  onApply: () => void
}

const ACTION_LABELS: Record<string, string> = { add: '新增', update: '更新', preserve: '保留', delete_candidate: '待删除', block: '阻断' }
const BLOCKER_LABELS: Record<string, string> = {
  ALARM_PLAN_BLOCKED: '计划含有阻断项，无法应用。',
  ALARM_BATCH_LIMIT_EXCEEDED: '展开后的定义数量超过单次上限。',
  ALARM_ENTITY_UNRESOLVED: '存在无法解析到已确认实体实例的对象。',
  ALARM_MIGRATION_AMBIGUOUS: '存在需要工程师明确选择实体实例的旧配置。',
}

function blockerLabel(blocker: string): string {
  return BLOCKER_LABELS[blocker] || '存在服务端阻断项，请检查实体范围与规则条件。'
}

function actionLabel(item: AlarmConfigurationPlanItem): string {
  return ACTION_LABELS[item.action] || '待处理'
}

export default function PlanPreview({ plan, entityNames, applying, stale, onApply }: Props) {
  const [filter, setFilter] = useState('all')
  const counts = useMemo(() => plan.items.reduce<Record<string, number>>((result, item) => ({ ...result, [item.action]: (result[item.action] || 0) + 1 }), {}), [plan.items])
  const visibleItems = filter === 'all' ? plan.items : plan.items.filter((item) => item.action === filter)
  const blockers = [...plan.blockers, ...plan.items.flatMap((item) => item.blockers)]
  const uniqueBlockers = Array.from(new Set(blockers))
  const isBlocked = plan.status === 'blocked' || uniqueBlockers.length > 0

  return (
    <section aria-labelledby="alarm-plan-heading" className="neu-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 id="alarm-plan-heading" className="text-sm font-bold text-gray-800">变更预览</h3>
          <p className="mt-0.5 text-xs text-gray-500">本计划共展开 {plan.items.length.toLocaleString()} 项定义变更，应用前不会写入现场配置。</p>
        </div>
        <button type="button" onClick={onApply} disabled={isBlocked || stale || applying} className="rounded-lg bg-[#52c41a] px-4 py-2 text-xs font-semibold text-white shadow-sm hover:bg-[#389e0d] focus:outline-none focus:ring-2 focus:ring-[#52c41a]/60 disabled:cursor-not-allowed disabled:opacity-50">
          {applying ? '正在应用...' : stale ? '计划已失效，请重新生成' : isBlocked ? '存在阻断项' : '确认应用配置'}
        </button>
      </div>

      {(isBlocked || stale) && <div role="alert" className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">{stale ? '配置基线已经变化。请由实施工程师重新生成计划并再次确认，系统不会自动应用新计划。' : uniqueBlockers.map(blockerLabel).join(' ')}</div>}

      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" onClick={() => setFilter('all')} className={`rounded-md border px-2 py-1 text-xs ${filter === 'all' ? 'border-[#52c41a] bg-[#52c41a]/10 text-[#287c12]' : 'border-gray-200 text-gray-600 hover:bg-white/50'}`}>全部 {plan.items.length}</button>
        {Object.entries(counts).map(([action, count]) => <button key={action} type="button" onClick={() => setFilter(action)} className={`rounded-md border px-2 py-1 text-xs ${filter === action ? 'border-[#52c41a] bg-[#52c41a]/10 text-[#287c12]' : 'border-gray-200 text-gray-600 hover:bg-white/50'}`}>{ACTION_LABELS[action] || '待处理'} {count}</button>)}
      </div>

      <div className="mt-3 max-h-[420px] overflow-auto rounded-lg border border-white/70">
        <table className="w-full min-w-[680px] text-left text-xs">
          <thead className="sticky top-0 bg-[#f0f0f0] text-[11px] text-gray-500"><tr className="border-b border-gray-200"><th className="px-3 py-2">实体实例</th><th className="px-3 py-2">规则</th><th className="px-3 py-2">定义</th><th className="px-3 py-2">动作</th><th className="px-3 py-2">状态</th></tr></thead>
          <tbody>
            {visibleItems.map((item, index) => <tr key={`${item.entity_instance_id}-${item.rule_id}-${index}`} className="border-b border-white/70 last:border-b-0"><td className="px-3 py-2 font-medium text-gray-700">{entityNames.get(item.entity_instance_id) || '已选实体实例'}</td><td className="px-3 py-2 text-gray-600">{item.rule_id}</td><td className="px-3 py-2 text-gray-500">{item.definition_key}</td><td className="px-3 py-2"><span className="rounded border border-gray-200 bg-white/40 px-1.5 py-0.5 text-[11px] text-gray-700">{actionLabel(item)}</span></td><td className="px-3 py-2 text-gray-500">{item.blockers.length ? item.blockers.map(blockerLabel).join(' ') : '可处理'}</td></tr>)}
            {visibleItems.length === 0 && <tr><td colSpan={5} className="px-3 py-6 text-center text-xs text-gray-400">当前筛选没有变更项。</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  )
}
