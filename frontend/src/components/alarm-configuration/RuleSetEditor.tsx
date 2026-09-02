import type { AlarmConditionOperator, AlarmRule, AlarmSeverity } from '../../api/client'

interface Props {
  rules: AlarmRule[]
  onChange: (rules: AlarmRule[]) => void
  disabled?: boolean
}

const OPERATORS: { value: AlarmConditionOperator; label: string }[] = [
  { value: 'gt', label: '大于' }, { value: 'gte', label: '大于等于' }, { value: 'lt', label: '小于' },
  { value: 'lte', label: '小于等于' }, { value: 'eq', label: '等于' }, { value: 'ne', label: '不等于' },
]

const SEVERITIES: { value: AlarmSeverity; label: string; className: string }[] = [
  { value: 'CRITICAL', label: '严重', className: 'bg-red-100 text-red-700 border-red-200' },
  { value: 'MAJOR', label: '重要', className: 'bg-orange-100 text-orange-700 border-orange-200' },
  { value: 'WARNING', label: '警告', className: 'bg-amber-100 text-amber-800 border-amber-200' },
  { value: 'INFO', label: '提示', className: 'bg-sky-100 text-sky-700 border-sky-200' },
]

export function ruleValidation(rules: AlarmRule[]): string | null {
  if (rules.length === 0) return '请至少添加一条告警规则。'
  const ids = rules.map((rule) => rule.id.trim())
  if (ids.some((id) => !id)) return '每条规则都需要稳定标识。'
  if (new Set(ids).size !== ids.length) return '规则稳定标识不能重复。'
  if (rules.some((rule) => !rule.name.trim())) return '每条规则都需要名称。'
  if (rules.some((rule) => [rule.trigger.value, rule.recovery.value].some((value) => !Number.isFinite(value)) || [rule.trigger_duration_seconds, rule.recovery_duration_seconds, rule.notification_throttle_seconds].some((value) => !Number.isFinite(value) || value < 0))) return '阈值必须为有限数字，持续时间和通知间隔必须为非负数字。'
  return null
}

function freshRule(index: number): AlarmRule {
  return {
    id: `rule-${index}`, name: `规则 ${index}`, severity: 'WARNING',
    trigger: { operator: 'gte', value: 0 }, trigger_duration_seconds: 0,
    recovery: { operator: 'lt', value: 0 }, recovery_duration_seconds: 0,
    notification_throttle_seconds: 300, unit: null, fault_map_id: null,
  }
}

export default function RuleSetEditor({ rules, onChange, disabled }: Props) {
  const validation = ruleValidation(rules)
  const patchRule = (index: number, patch: Partial<AlarmRule>) => onChange(rules.map((rule, itemIndex) => itemIndex === index ? { ...rule, ...patch } : rule))
  const patchCondition = (index: number, kind: 'trigger' | 'recovery', patch: Partial<AlarmRule['trigger']>) => {
    const rule = rules[index]
    patchRule(index, { [kind]: { ...rule[kind], ...patch } })
  }

  return (
    <section aria-labelledby="alarm-rules-heading" className="neu-card p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 id="alarm-rules-heading" className="text-sm font-bold text-gray-800">规则修订</h3>
          <p className="mt-0.5 text-xs text-gray-500">编辑仅保留在当前工作台，生成计划时才创建规则修订。</p>
        </div>
        <button type="button" onClick={() => onChange([...rules, freshRule(rules.length + 1)])} disabled={disabled || rules.length >= 20} className="neu-btn px-3 py-1.5 text-xs font-medium text-[#287c12] disabled:cursor-not-allowed disabled:opacity-50">添加规则</button>
      </div>
      {validation && <div role="alert" className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">{validation}</div>}
      <div className="mt-3 space-y-3 md:hidden">{rules.map((rule, index) => <article key={index} className="rounded border border-white/70 p-3 text-xs"><div className="mb-2 flex items-center justify-between"><strong>规则 {index + 1}</strong><button type="button" onClick={() => onChange(rules.filter((_, itemIndex) => itemIndex !== index))} disabled={disabled} className="rounded px-2 py-1 text-red-600 disabled:opacity-50">移除</button></div><div className="grid gap-2"><label>标识<input aria-label={`规则 ${index + 1} 标识`} value={rule.id} onChange={(event) => patchRule(index, { id: event.target.value })} disabled={disabled} className="neu-input mt-1 w-full px-2 py-1.5" /></label><label>名称<input aria-label={`规则 ${index + 1} 名称`} value={rule.name} onChange={(event) => patchRule(index, { name: event.target.value })} disabled={disabled} className="neu-input mt-1 w-full px-2 py-1.5" /></label><label>严重度<select aria-label={`规则 ${index + 1} 严重度`} value={rule.severity} onChange={(event) => patchRule(index, { severity: event.target.value as AlarmSeverity })} disabled={disabled} className="neu-input mt-1 w-full px-2 py-1.5">{SEVERITIES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label><div className="space-y-2"><label className="block">触发条件<select aria-label={`规则 ${index + 1} 触发运算符`} value={rule.trigger.operator} onChange={(event) => patchCondition(index, 'trigger', { operator: event.target.value as AlarmConditionOperator })} disabled={disabled} className="neu-input mt-1 w-full px-2 py-1.5">{OPERATORS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select><input aria-label={`规则 ${index + 1} 触发值`} type="number" value={typeof rule.trigger.value === 'boolean' ? '' : rule.trigger.value} onChange={(event) => patchCondition(index, 'trigger', { value: Number(event.target.value) })} disabled={disabled} className="neu-input mt-1 w-full px-2 py-1.5" /></label><label className="block">恢复条件<select aria-label={`规则 ${index + 1} 恢复运算符`} value={rule.recovery.operator} onChange={(event) => patchCondition(index, 'recovery', { operator: event.target.value as AlarmConditionOperator })} disabled={disabled} className="neu-input mt-1 w-full px-2 py-1.5">{OPERATORS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select><input aria-label={`规则 ${index + 1} 恢复值`} type="number" value={typeof rule.recovery.value === 'boolean' ? '' : rule.recovery.value} onChange={(event) => patchCondition(index, 'recovery', { value: Number(event.target.value) })} disabled={disabled} className="neu-input mt-1 w-full px-2 py-1.5" /></label></div><div className="space-y-2"><label className="block">触发持续<input aria-label={`规则 ${index + 1} 触发持续秒数`} type="number" min="0" value={rule.trigger_duration_seconds} onChange={(event) => patchRule(index, { trigger_duration_seconds: Number(event.target.value) })} disabled={disabled} className="neu-input mt-1 w-full px-2 py-1.5" /></label><label className="block">恢复持续<input aria-label={`规则 ${index + 1} 恢复持续秒数`} type="number" min="0" value={rule.recovery_duration_seconds} onChange={(event) => patchRule(index, { recovery_duration_seconds: Number(event.target.value) })} disabled={disabled} className="neu-input mt-1 w-full px-2 py-1.5" /></label><label className="block">通知间隔<input aria-label={`规则 ${index + 1} 通知间隔秒数`} type="number" min="0" value={rule.notification_throttle_seconds} onChange={(event) => patchRule(index, { notification_throttle_seconds: Number(event.target.value) })} disabled={disabled} className="neu-input mt-1 w-full px-2 py-1.5" /></label></div></div></article>)}{rules.length === 0 && <p className="py-6 text-center text-gray-400">添加至少一条规则后才能生成计划。</p>}</div>
      <div className="mt-3 hidden md:block">
        <table className="w-full text-left text-xs">
          <thead className="text-[11px] text-gray-500"><tr className="border-b border-gray-200"><th className="px-2 py-2">标识</th><th className="px-2 py-2">名称</th><th className="px-2 py-2">严重度</th><th className="px-2 py-2">触发条件</th><th className="px-2 py-2">触发持续</th><th className="px-2 py-2">恢复条件</th><th className="px-2 py-2">恢复持续</th><th className="px-2 py-2">通知间隔</th><th className="px-2 py-2" /></tr></thead>
          <tbody>
            {rules.map((rule, index) => (
              <tr key={`${rule.id}-${index}`} className="border-b border-white/70 last:border-b-0">
                <td className="p-1.5"><input aria-label={`规则 ${index + 1} 标识`} value={rule.id} onChange={(event) => patchRule(index, { id: event.target.value })} disabled={disabled} className="neu-input w-28 px-2 py-1.5 text-xs" /></td>
                <td className="p-1.5"><input aria-label={`规则 ${index + 1} 名称`} value={rule.name} onChange={(event) => patchRule(index, { name: event.target.value })} disabled={disabled} className="neu-input w-28 px-2 py-1.5 text-xs" /></td>
                <td className="p-1.5"><select aria-label={`规则 ${index + 1} 严重度`} value={rule.severity} onChange={(event) => patchRule(index, { severity: event.target.value as AlarmSeverity })} disabled={disabled} className={`neu-input w-20 px-2 py-1.5 text-xs ${SEVERITIES.find((item) => item.value === rule.severity)?.className || ''}`}>{SEVERITIES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></td>
                <td className="p-1.5"><div className="flex gap-1"><select aria-label={`规则 ${index + 1} 触发运算符`} value={rule.trigger.operator} onChange={(event) => patchCondition(index, 'trigger', { operator: event.target.value as AlarmConditionOperator })} disabled={disabled} className="neu-input w-20 px-1 py-1.5 text-xs">{OPERATORS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select><input aria-label={`规则 ${index + 1} 触发值`} type="number" value={typeof rule.trigger.value === 'boolean' ? '' : rule.trigger.value} onChange={(event) => patchCondition(index, 'trigger', { value: Number(event.target.value) })} disabled={disabled} className="neu-input w-20 px-2 py-1.5 text-xs" /></div></td>
                <td className="p-1.5"><input aria-label={`规则 ${index + 1} 触发持续秒数`} type="number" min="0" value={rule.trigger_duration_seconds} onChange={(event) => patchRule(index, { trigger_duration_seconds: Number(event.target.value) })} disabled={disabled} className="neu-input w-20 px-2 py-1.5 text-xs" /></td>
                <td className="p-1.5"><div className="flex gap-1"><select aria-label={`规则 ${index + 1} 恢复运算符`} value={rule.recovery.operator} onChange={(event) => patchCondition(index, 'recovery', { operator: event.target.value as AlarmConditionOperator })} disabled={disabled} className="neu-input w-20 px-1 py-1.5 text-xs">{OPERATORS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select><input aria-label={`规则 ${index + 1} 恢复值`} type="number" value={typeof rule.recovery.value === 'boolean' ? '' : rule.recovery.value} onChange={(event) => patchCondition(index, 'recovery', { value: Number(event.target.value) })} disabled={disabled} className="neu-input w-20 px-2 py-1.5 text-xs" /></div></td>
                <td className="p-1.5"><input aria-label={`规则 ${index + 1} 恢复持续秒数`} type="number" min="0" value={rule.recovery_duration_seconds} onChange={(event) => patchRule(index, { recovery_duration_seconds: Number(event.target.value) })} disabled={disabled} className="neu-input w-20 px-2 py-1.5 text-xs" /></td>
                <td className="p-1.5"><input aria-label={`规则 ${index + 1} 通知间隔秒数`} type="number" min="0" value={rule.notification_throttle_seconds} onChange={(event) => patchRule(index, { notification_throttle_seconds: Number(event.target.value) })} disabled={disabled} className="neu-input w-20 px-2 py-1.5 text-xs" /></td>
                <td className="p-1.5"><button type="button" onClick={() => onChange(rules.filter((_, itemIndex) => itemIndex !== index))} disabled={disabled} className="rounded px-2 py-1 text-xs text-red-600 hover:bg-red-50 disabled:opacity-50">移除</button></td>
              </tr>
            ))}
            {rules.length === 0 && <tr><td colSpan={9} className="px-3 py-6 text-center text-xs text-gray-400">添加至少一条规则后才能生成计划。</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  )
}
