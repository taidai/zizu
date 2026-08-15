import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlarmConfigurationApiError, applyAlarmConfigurationPlan, createAlarmConfigurationPlan, createAlarmRuleSet,
  createAlarmRuleSetRevision, createLegacyAlarmMigrationPlan, getUnifiedAlarmConfiguration, fetchAlarmRuleSets,
  fetchEntityInstances, fetchLegacyAlarmMigrationCandidates, type AlarmConfigurationCurrent,
  type AlarmConfigurationPlan, type AlarmRule, type AlarmRuleSetRevision, type EntityInstance,
  type LegacyAlarmMigrationCandidate,
} from '../api/client'
import EntityScopePicker, { type AlarmEntityScope } from '../components/alarm-configuration/EntityScopePicker'
import LegacyMigrationPanel from '../components/alarm-configuration/LegacyMigrationPanel'
import PlanPreview from '../components/alarm-configuration/PlanPreview'
import RuleSetEditor, { ruleValidation } from '../components/alarm-configuration/RuleSetEditor'

const EMPTY_SCOPE: AlarmEntityScope = { entity_instance_ids: [], device_instance_ids: [], entity_definition_ids: [] }

function defaultRule(): AlarmRule {
  return { id: 'threshold-warning', name: '阈值告警', severity: 'WARNING', trigger: { operator: 'gte', value: 0 }, trigger_duration_seconds: 0, recovery: { operator: 'lt', value: 0 }, recovery_duration_seconds: 0, notification_throttle_seconds: 300, unit: null, fault_map_id: null }
}

function sameRules(left: AlarmRule[], right: AlarmRule[]): boolean { return JSON.stringify(left) === JSON.stringify(right) }

export default function AlarmConfigurationPage() {
  const [current, setCurrent] = useState<AlarmConfigurationCurrent | null>(null)
  const [ruleSets, setRuleSets] = useState<AlarmRuleSetRevision[]>([])
  const [entities, setEntities] = useState<EntityInstance[]>([])
  const [migrationInstallationId, setMigrationInstallationId] = useState('')
  const [migrationCandidates, setMigrationCandidates] = useState<LegacyAlarmMigrationCandidate[]>([])
  const [scope, setScope] = useState<AlarmEntityScope>(EMPTY_SCOPE)
  const [selectedRuleSetId, setSelectedRuleSetId] = useState('')
  const [rules, setRules] = useState<AlarmRule[]>([defaultRule()])
  const [newRuleSetName, setNewRuleSetName] = useState('现场告警规则')
  const [plan, setPlan] = useState<AlarmConfigurationPlan | null>(null)
  const [migrationSelections, setMigrationSelections] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [planning, setPlanning] = useState(false)
  const [applying, setApplying] = useState(false)
  const [migrating, setMigrating] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [planStale, setPlanStale] = useState(false)
  const [idempotencyKey, setIdempotencyKey] = useState<string | null>(null)
  const selectedRuleSet = ruleSets.find((item) => item.rule_set_id === selectedRuleSetId) || null
  const entityNames = useMemo(() => new Map(entities.map((entity) => [entity.id, `${entity.device_display_name} / ${entity.display_name}`])), [entities])
  const scopeHasValue = scope.entity_instance_ids.length + scope.device_instance_ids.length + scope.entity_definition_ids.length > 0

  const load = useCallback(async () => {
    setLoading(true); setError('')
    try {
      const [configuration, revisions, entityResponse, migrations] = await Promise.all([getUnifiedAlarmConfiguration(), fetchAlarmRuleSets(), fetchEntityInstances(), fetchLegacyAlarmMigrationCandidates()])
      setCurrent(configuration); setRuleSets(revisions); setEntities(entityResponse.items); setMigrationInstallationId(migrations.installation_id); setMigrationCandidates(migrations.items); setMigrationSelections({})
      if (!selectedRuleSetId && revisions[0]) { setSelectedRuleSetId(revisions[0].rule_set_id); setRules(revisions[0].rules) }
    } catch { setError('无法读取告警配置工作台数据，请检查平台连接与权限后重试。') } finally { setLoading(false) }
  }, [selectedRuleSetId])

  useEffect(() => { void load() }, [load])

  const resetPlan = () => { setPlan(null); setPlanStale(false); setIdempotencyKey(null) }
  const chooseRuleSet = (id: string) => { setSelectedRuleSetId(id); const revision = ruleSets.find((item) => item.rule_set_id === id); if (revision) setRules(revision.rules); resetPlan() }

  const createPlan = async () => {
    if (!migrationInstallationId || !scopeHasValue) { setError('请先选择至少一个配置范围。'); return }
    const validation = ruleValidation(rules)
    if (validation) { setError(validation); return }
    setPlanning(true); setError(''); setSuccess('')
    try {
      let revision = selectedRuleSet
      if (!revision) {
        revision = await createAlarmRuleSet({ key: `workspace-${Date.now()}`, name: newRuleSetName.trim() || '现场告警规则', rules })
        setRuleSets((items) => [revision!, ...items]); setSelectedRuleSetId(revision.rule_set_id)
      } else if (!sameRules(rules, revision.rules)) {
        revision = await createAlarmRuleSetRevision(revision.rule_set_id, rules)
        setRuleSets((items) => items.map((item) => item.rule_set_id === revision!.rule_set_id ? revision! : item))
      }
      const created = await createAlarmConfigurationPlan({ installation_id: migrationInstallationId, selection: scope, rule_set_id: revision.rule_set_id, rule_set_revision: revision.revision })
      setPlan(created); setPlanStale(false); setIdempotencyKey(null)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '生成计划失败。') } finally { setPlanning(false) }
  }

  const applyPlan = async () => {
    if (!plan) return
    const key = idempotencyKey || crypto.randomUUID()
    setIdempotencyKey(key); setApplying(true); setError(''); setSuccess('')
    try {
      const result = await applyAlarmConfigurationPlan(plan.id, plan.digest, key)
      setSuccess(`已应用告警配置，站点配置版本已更新至 ${result.site_configuration_version}。`); setPlan(null); setIdempotencyKey(null); await load()
    } catch (reason) {
      const stale = reason instanceof AlarmConfigurationApiError && ['ALARM_PLAN_STALE', 'ALARM_PLAN_DIGEST_MISMATCH'].includes(reason.code || '')
      if (stale) setPlanStale(true)
      setError(reason instanceof Error ? reason.message : '应用配置计划失败。')
    } finally { setApplying(false) }
  }

  const migrate = async () => {
    const selections = migrationCandidates.filter((candidate) => candidate.status === 'ambiguous').map((candidate) => ({ source_kind: candidate.source_kind, source_key: candidate.source_key, entity_instance_id: migrationSelections[`${candidate.source_kind}:${candidate.source_key}`] })).filter((selection) => selection.entity_instance_id)
    setMigrating(true); setError(''); setSuccess('')
    try { await createLegacyAlarmMigrationPlan({ installation_id: migrationInstallationId, selections }); setSuccess('旧告警配置已按明确选择迁移，原始配置保持只读。'); await load() } catch (reason) { setError(reason instanceof Error ? reason.message : '旧配置迁移失败。') } finally { setMigrating(false) }
  }

  if (loading) return <div className="neu-card p-8 text-center text-sm text-gray-500">正在加载统一告警配置工作台...</div>

  return <div className="space-y-4">
    <header className="flex flex-wrap items-end justify-between gap-3"><div><h2 className="text-base font-bold text-gray-800">统一告警配置</h2><p className="mt-1 text-xs text-gray-500">面向实施工程师的预览优先工作台。严重度固定为严重、重要、警告、提示。</p></div><div className="flex items-center gap-3 text-xs text-gray-500"><span>当前配置版本 {current?.site_configuration_version ?? '未加载'}</span><span>旧配置候选 {migrationCandidates.length}</span><button type="button" onClick={() => void load()} className="neu-btn px-3 py-1.5 text-xs" disabled={loading}>刷新</button></div></header>
    {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">{error}</div>}
    {success && <div role="status" className="rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-800">{success}</div>}
    <section aria-labelledby="rule-set-heading" className="neu-card p-4"><div className="grid gap-3 md:grid-cols-2"><label className="text-xs font-medium text-gray-700">使用已有规则集<select aria-label="选择已有规则集" value={selectedRuleSetId} onChange={(event) => chooseRuleSet(event.target.value)} className="neu-input mt-1 block w-full px-3 py-2 text-xs"><option value="">新建规则集</option>{ruleSets.map((item) => <option key={item.rule_set_id} value={item.rule_set_id}>{item.name}（第 {item.revision} 版）</option>)}</select></label>{!selectedRuleSetId ? <label className="text-xs font-medium text-gray-700">新规则集名称<input value={newRuleSetName} onChange={(event) => setNewRuleSetName(event.target.value)} className="neu-input mt-1 block w-full px-3 py-2 text-xs" maxLength={200} /></label> : <p className="self-end pb-2 text-xs text-gray-500">修改规则后会在生成计划时创建新的修订，不会覆盖既有版本。</p>}</div></section>
    <EntityScopePicker entities={entities} value={scope} onChange={(next) => { setScope(next); resetPlan() }} disabled={planning || applying} />
    <RuleSetEditor rules={rules} onChange={(next) => { setRules(next); resetPlan() }} disabled={planning || applying} />
    <div className="flex justify-end"><button type="button" onClick={() => void createPlan()} disabled={planning || applying || !scopeHasValue} className="rounded-lg bg-[#52c41a] px-4 py-2 text-xs font-semibold text-white shadow-sm hover:bg-[#389e0d] focus:outline-none focus:ring-2 focus:ring-[#52c41a]/60 disabled:cursor-not-allowed disabled:opacity-50">{planning ? '正在生成计划...' : '生成变更预览'}</button></div>
    {plan && <PlanPreview plan={plan} entityNames={entityNames} applying={applying} stale={planStale} onApply={() => void applyPlan()} />}
    <LegacyMigrationPanel candidates={migrationCandidates} entities={entities} selections={migrationSelections} onSelectionChange={(sourceKind, sourceKey, entityInstanceId) => setMigrationSelections((previous) => ({ ...previous, [`${sourceKind}:${sourceKey}`]: entityInstanceId }))} onMigrate={() => void migrate()} migrating={migrating} />
  </div>
}
