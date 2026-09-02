import { useCallback, useEffect, useState } from 'react'
import {
  applyAlarmConfigurationPlan,
  createAlarmConfigurationPlan,
  createAlarmRuleSet,
  createAlarmRuleSetRevision,
  fetchAlarmRuleSets,
  fetchEntityInstances,
  getUnifiedAlarmConfiguration,
  type AlarmConfigurationCurrent,
  type AlarmConfigurationPlan,
  type AlarmRule,
  type AlarmRuleSetRevision,
  type EntityInstance,
} from '../api/client'
import RuleSetEditor, { ruleValidation } from '../components/alarm-configuration/RuleSetEditor'

const defaultRule = (): AlarmRule => ({
  id: 'threshold-warning', name: '阈值告警', severity: 'WARNING',
  trigger: { operator: 'gte', value: 0 }, trigger_duration_seconds: 0,
  recovery: { operator: 'lt', value: 0 }, recovery_duration_seconds: 0,
  notification_throttle_seconds: 300, unit: null, fault_map_id: null,
  http_notification_config_id: null,
})
const revisionKey = (item: AlarmRuleSetRevision) => `${item.rule_set_id}:${item.revision}`

export default function AlarmConfigurationPage(_props: { actorId: string; onOpenAlarms: () => void }) {
  const [current, setCurrent] = useState<AlarmConfigurationCurrent | null>(null)
  const [ruleSets, setRuleSets] = useState<AlarmRuleSetRevision[]>([])
  const [entities, setEntities] = useState<EntityInstance[]>([])
  const [selectedRevision, setSelectedRevision] = useState('')
  const [selectedEntities, setSelectedEntities] = useState<string[]>([])
  const [rules, setRules] = useState<AlarmRule[]>([defaultRule()])
  const [name, setName] = useState('现场告警规则')
  const [plan, setPlan] = useState<AlarmConfigurationPlan | null>(null)
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setBusy('load'); setError('')
    try {
      const [configuration, revisions, entityResponse] = await Promise.all([
        getUnifiedAlarmConfiguration(), fetchAlarmRuleSets(), fetchEntityInstances(),
      ])
      setCurrent(configuration); setRuleSets(revisions); setEntities(entityResponse.items)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取告警配置。')
    } finally { setBusy('') }
  }, [])

  useEffect(() => { void load() }, [load])

  const chooseRevision = (key: string) => {
    setSelectedRevision(key); setPlan(null)
    const revision = ruleSets.find((item) => revisionKey(item) === key)
    if (revision) { setRules(revision.rules); setName(revision.name) }
  }

  const preview = async () => {
    const validation = ruleValidation(rules)
    if (validation) { setError(validation); return }
    if (!selectedEntities.length) { setError('请至少选择一个 L2 全局实体。'); return }
    setBusy('plan'); setError(''); setMessage('')
    try {
      let revision = ruleSets.find((item) => revisionKey(item) === selectedRevision)
      if (!revision) {
        revision = await createAlarmRuleSet({ key: `alarm-${Date.now()}`, name: name.trim() || '现场告警规则', rules })
      } else if (JSON.stringify(revision.rules) !== JSON.stringify(rules)) {
        revision = await createAlarmRuleSetRevision(revision.rule_set_id, rules)
      }
      setRuleSets((items) => items.some((item) => revisionKey(item) === revisionKey(revision!)) ? items : [...items, revision!])
      setSelectedRevision(revisionKey(revision))
      setPlan(await createAlarmConfigurationPlan({
        selection: { entity_instance_ids: selectedEntities, node_ids: [], entity_definition_ids: [] },
        rule_set_id: revision.rule_set_id,
        rule_set_revision: revision.revision,
      }))
    } catch (reason) { setError(reason instanceof Error ? reason.message : '生成预览失败。') }
    finally { setBusy('') }
  }

  const apply = async () => {
    if (!plan) return
    setBusy('apply'); setError(''); setMessage('')
    try {
      const result = await applyAlarmConfigurationPlan(plan.id, plan.digest, crypto.randomUUID())
      setMessage(`告警配置已发布，统一配置版本 ${result.configuration_revision}。`)
      setPlan(null); await load()
    } catch (reason) { setError(reason instanceof Error ? reason.message : '发布失败。') }
    finally { setBusy('') }
  }

  if (busy === 'load' && !current) return <div className="neu-card p-8 text-center text-sm text-gray-500">正在加载告警配置...</div>
  return <div className="space-y-4">
    <header className="flex items-end justify-between">
      <div><h2 className="text-base font-bold text-gray-800">告警配置</h2><p className="mt-1 text-xs text-gray-500">规则只绑定 L2 全局实体；预览不改配置，发布才推进统一版本。</p></div>
      <span className="text-xs text-gray-500">配置版本 {current?.configuration_revision ?? '—'}</span>
    </header>
    {error && <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">{error}</div>}
    {message && <div className="rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-800">{message}</div>}
    <section className="neu-card p-4"><h3 className="text-sm font-bold text-gray-800">当前定义</h3><div className="mt-3 grid gap-2 md:grid-cols-2">{current?.definitions.map((item, index) => <div key={`${item.entity_display_name}-${index}`} className="rounded-lg border border-white/70 p-3 text-xs"><strong>{item.entity_display_name}</strong><p className="mt-1 text-gray-600">{item.rule_name} · {item.severity}</p></div>)}{!current?.definitions.length && <p className="text-xs text-gray-400">暂无告警定义。</p>}</div></section>
    <section className="neu-card p-4"><div className="grid gap-3 md:grid-cols-2"><label className="text-xs font-medium text-gray-700">规则集<select value={selectedRevision} onChange={(event) => chooseRevision(event.target.value)} className="neu-input mt-1 block w-full px-3 py-2"><option value="">新建规则集</option>{ruleSets.map((item) => <option key={revisionKey(item)} value={revisionKey(item)}>{item.name} · 第 {item.revision} 版</option>)}</select></label><label className="text-xs font-medium text-gray-700">名称<input value={name} onChange={(event) => setName(event.target.value)} className="neu-input mt-1 block w-full px-3 py-2" /></label></div></section>
    <section className="neu-card p-4"><h3 className="text-sm font-bold text-gray-800">选择 L2 全局实体</h3><div className="mt-3 grid gap-2 md:grid-cols-2">{entities.map((entity) => <label key={entity.id} className="flex items-center gap-2 rounded-lg border border-white/70 p-3 text-xs"><input type="checkbox" checked={selectedEntities.includes(entity.id)} onChange={(event) => setSelectedEntities((items) => event.target.checked ? [...items, entity.id] : items.filter((id) => id !== entity.id))} /><span><strong>{entity.node_display_name}</strong> / {entity.display_name}<span className="ml-2 text-gray-400">{entity.definition_id}</span></span></label>)}</div></section>
    <RuleSetEditor rules={rules} onChange={(next) => { setRules(next); setPlan(null) }} disabled={!!busy} />
    <div className="flex justify-end"><button onClick={() => void preview()} disabled={!!busy} className="rounded-lg bg-[#52c41a] px-4 py-2 text-xs font-semibold text-white disabled:opacity-50">{busy === 'plan' ? '生成中...' : '生成变更预览'}</button></div>
    {plan && <section className="neu-card p-4"><h3 className="text-sm font-bold text-gray-800">变更预览</h3><p className="mt-2 text-xs text-gray-500">基线版本 {plan.base_configuration_revision} · {plan.items.length} 项 · {plan.status}</p><div className="mt-3 space-y-2">{plan.items.map((item) => <div key={item.definition_key} className="rounded-lg border border-white/70 p-2 text-xs"><strong>{item.action}</strong><span className="ml-2 text-gray-600">{item.definition_key}</span></div>)}</div><button onClick={() => void apply()} disabled={busy === 'apply' || plan.status !== 'ready'} className="mt-4 rounded-lg bg-[#52c41a] px-4 py-2 text-xs font-semibold text-white disabled:opacity-50">{busy === 'apply' ? '发布中...' : '确认发布'}</button></section>}
  </div>
}
