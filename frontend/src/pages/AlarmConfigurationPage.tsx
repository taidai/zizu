import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlarmConfigurationApiError, applyAlarmConfigurationPlan, createAlarmConfigurationPlan, createAlarmRuleSet, createAlarmRuleSetRevision, createLegacyAlarmMigrationPlan, fetchAlarmConfigurationAcceptanceProgress, fetchAlarmConfigurationReport, fetchAlarmRuleSets, fetchEntityInstances, fetchLegacyAlarmMigrationCandidates, getAlarmConfigurationPlan, getUnifiedAlarmConfiguration, runAlarmConfigurationAcceptance, type AlarmConfigurationAcceptanceProgress, type AlarmConfigurationAcceptanceReport, type AlarmConfigurationCurrent, type AlarmConfigurationPlan, type AlarmRule, type AlarmRuleSetRevision, type EntityInstance, type LegacyAlarmMigrationCandidate } from '../api/client'
import { formatAlarmConditionValue, isDefinitiveAlarmApplyCode } from '../components/alarm-configuration/alarmConfigurationContracts'
import { clearAcceptanceRetry, readAcceptanceRetry, refreshAppliedWorkspace, saveAcceptanceRetry } from '../components/alarm-configuration/acceptanceRetryState'
import EntityScopePicker, { type AlarmEntityScope } from '../components/alarm-configuration/EntityScopePicker'
import LegacyMigrationPanel from '../components/alarm-configuration/LegacyMigrationPanel'
import PlanPreview from '../components/alarm-configuration/PlanPreview'
import RuleSetEditor, { ruleValidation } from '../components/alarm-configuration/RuleSetEditor'
import { canReplaySavedApply, clearApplyContext, clearWorkspaceContext, readWorkspaceContext, saveApplyContext, savePlanContext } from '../components/alarm-configuration/workspaceState'

const EMPTY_SCOPE: AlarmEntityScope = { entity_instance_ids: [], device_instance_ids: [], entity_definition_ids: [] }
const severityLabel: Record<string, string> = { CRITICAL: '严重', MAJOR: '重要', WARNING: '警告', INFO: '提示' }
const operatorLabel: Record<string, string> = { gt: '大于', gte: '大于等于', lt: '小于', lte: '小于等于', eq: '等于', ne: '不等于' }
const defaultRule = (): AlarmRule => ({ id: 'threshold-warning', name: '阈值告警', severity: 'WARNING', trigger: { operator: 'gte', value: 0 }, trigger_duration_seconds: 0, recovery: { operator: 'lt', value: 0 }, recovery_duration_seconds: 0, notification_throttle_seconds: 300, unit: null, fault_map_id: null })
const sameRules = (left: AlarmRule[], right: AlarmRule[]) => JSON.stringify(left) === JSON.stringify(right)
const revisionKey = (revision: AlarmRuleSetRevision) => `${revision.rule_set_id}:${revision.revision}`
const unrecoverableReplay = (reason: unknown) => reason instanceof AlarmConfigurationApiError && ['ALARM_PLAN_NOT_FOUND', 'ALARM_PLAN_DIGEST_MISMATCH'].includes(reason.code || '')
const stalePlan = (reason: unknown) => reason instanceof AlarmConfigurationApiError && reason.code === 'ALARM_PLAN_STALE'
const definitiveApplyFailure = (reason: unknown) => reason instanceof AlarmConfigurationApiError && isDefinitiveAlarmApplyCode(reason.code)
const alarmWorkspaceError = (reason: unknown, fallback: string) => {
  if (reason instanceof AlarmConfigurationApiError) return reason.message
  console.error(fallback, reason)
  return fallback
}
const acceptanceStageLabel = { waiting_trigger: '待触发', waiting_acknowledgement: '待操作员在告警中心确认', waiting_recovery: '待现场恢复', passed: '通过' } as const
const eventStateLabel: Record<string, string> = { pending: '等待持续触发', active_unacknowledged: '活动未确认', active_acknowledged: '活动已确认', recovered: '已恢复' }
const transitionLabel: Record<string, string> = { ALARM_ACTIVATED: '已触发', ALARM_ACKNOWLEDGED: '已确认', ALARM_RECOVERED: '已恢复' }
const shortReference = (value: string | null) => value ? value.replace(/-/g, '').slice(0, 8).toUpperCase() : '未形成'
const progressOrEmpty = async () => {
  try { return await fetchAlarmConfigurationAcceptanceProgress() }
  catch (reason) { if (reason instanceof AlarmConfigurationApiError && reason.status === 404) return null; throw reason }
}

function AcceptancePanel({ progress, report, entityNames, loading, running, error, onRefresh, onGenerate, onOpenAlarms }: {
  progress: AlarmConfigurationAcceptanceProgress | null
  report: AlarmConfigurationAcceptanceReport | null
  entityNames: Map<string, string>
  loading: boolean
  running: boolean
  onRefresh: () => void
  onGenerate: () => void
  onOpenAlarms: () => void
  error?: string
}) {
  const visibleItems = progress?.items.filter((item) => item.action === 'add' || item.action === 'update') || []
  const progressByDefinition = new Map(progress?.items.map((item) => [item.definition_id, item]) || [])
  const waitingForAcknowledgement = visibleItems.some((item) => item.stage === 'waiting_acknowledgement')
  return <section aria-labelledby="alarm-acceptance-heading" className="neu-card p-4">
    <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
      <div><h3 id="alarm-acceptance-heading" className="text-sm font-bold text-gray-800">最新配置验收证据</h3><p className="mt-1 text-xs text-gray-500">这里只观察正常告警生命周期。确认操作仅在告警中心完成，现场恢复只能来自设备观测。</p></div>
      <div className="flex flex-col gap-2 sm:flex-row md:justify-end"><button type="button" onClick={onRefresh} disabled={loading || running} className="neu-btn px-3 py-2 text-xs disabled:cursor-not-allowed disabled:opacity-50">{loading ? '正在读取证据...' : '刷新证据'}</button><button type="button" onClick={onGenerate} disabled={!progress?.ready_to_report || !!progress?.report_id || loading || running} className="rounded-lg bg-[#52c41a] px-3 py-2 text-xs font-semibold text-white shadow-sm hover:bg-[#389e0d] focus:outline-none focus:ring-2 focus:ring-[#52c41a]/60 disabled:cursor-not-allowed disabled:opacity-50">{running ? '正在生成报告...' : progress?.report_id ? '验收报告已生成' : '生成验收报告'}</button></div>
    </div>
    {error && <div role="alert" className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">{error}</div>}
    {!progress && <div className="mt-3 rounded-lg border border-white/70 bg-white/25 px-3 py-4 text-xs text-gray-500">当前没有已应用的告警配置。应用计划后，服务端会在这里给出证据进度。</div>}
    {progress && <><div className="mt-3 flex flex-col gap-2 text-xs text-gray-600 sm:flex-row sm:items-center sm:justify-between"><span>站点配置版本 {progress.site_configuration_version}，{progress.ready_to_report ? '证据已完整' : '证据尚未完整'}</span>{waitingForAcknowledgement && <button type="button" onClick={onOpenAlarms} className="self-start font-semibold text-[#389e0d] underline decoration-[#52c41a]/50 underline-offset-4 focus:outline-none focus:ring-2 focus:ring-[#52c41a]/60 sm:self-auto">前往告警中心确认</button>}</div><div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2">{visibleItems.map((item) => <article key={item.definition_id} className={`rounded-lg border p-3 text-xs ${item.stage === 'passed' ? 'border-green-200 bg-green-50/70' : item.stage === 'waiting_acknowledgement' ? 'border-amber-200 bg-amber-50/70' : 'border-white/80 bg-white/25'}`}><div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between"><strong className="text-gray-800">{entityNames.get(item.entity_instance_id) || '已应用实体'} / {item.rule_name}</strong><span className="font-semibold text-gray-700">{acceptanceStageLabel[item.stage]}</span></div><p className="mt-2 text-gray-500">{item.action === 'add' ? '新增定义' : '更新定义'}，事件状态 {item.event_state ? eventStateLabel[item.event_state] || '证据不完整' : '尚未形成'}</p></article>)}{visibleItems.length === 0 && <p className="text-xs text-gray-500">本次应用没有需要重新触发的新增或更新定义。</p>}</div></>}
    {report && <div className="mt-4 border-t border-white/80 pt-4"><div className="grid grid-cols-1 gap-2 text-xs md:grid-cols-2"><p><span className="text-gray-500">报告编号</span><strong className="ml-2 text-gray-800">{shortReference(report.id)}</strong></p><p><span className="text-gray-500">总体结论</span><strong className="ml-2 text-gray-800">{report.status === 'passed' ? '通过' : '未通过'}</strong></p><p><span className="text-gray-500">站点配置版本</span><strong className="ml-2 text-gray-800">{report.site_configuration_version}</strong></p><p><span className="text-gray-500">完成时间</span><strong className="ml-2 text-gray-800">{new Date(report.finished_at).toLocaleString('zh-CN')}</strong></p><p className="md:col-span-2"><span className="text-gray-500">内容摘要</span><code className="mt-1 block break-all rounded bg-white/35 px-2 py-1 font-mono text-[11px] text-gray-700">{report.digest}</code></p></div><div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2">{report.items.map((item, index) => { const matched = progressByDefinition.get(item.definition_id); const pendingAt = typeof item.evidence.pending_at === 'string' ? item.evidence.pending_at : null; return <article key={item.definition_id} className="rounded-lg border border-white/80 bg-white/25 p-3 text-xs"><strong className="text-gray-800">{matched ? `${entityNames.get(matched.entity_instance_id) || '已应用实体'} / ${matched.rule_name}` : `验收定义 ${index + 1}`}</strong><dl className="mt-2 grid grid-cols-1 gap-1 text-gray-600"><div><dt className="inline text-gray-500">事件</dt><dd className="ml-2 inline">{shortReference(item.event_id)}，{item.event_state ? eventStateLabel[item.event_state] || '证据不完整' : '尚未形成'}</dd></div><div><dt className="inline text-gray-500">时间线</dt><dd className="ml-2 inline">{item.transition_codes.map((code) => transitionLabel[code]).filter(Boolean).join('、') || '证据不完整'}</dd></div><div><dt className="inline text-gray-500">确认审计</dt><dd className="ml-2 inline">{shortReference(item.acknowledgement_audit_event_id)}</dd></div>{pendingAt && <div><dt className="inline text-gray-500">证据起点</dt><dd className="ml-2 inline">{new Date(pendingAt).toLocaleString('zh-CN')}</dd></div>}</dl></article> })}</div><p className="mt-3 text-xs text-gray-500">报告由服务端依据完整事件、时间线与确认审计生成，内容摘要固定且不可修改。</p></div>}
  </section>
}

export default function AlarmConfigurationPage({ actorId, onOpenAlarms }: { actorId: string; onOpenAlarms: () => void }) {
  const [current, setCurrent] = useState<AlarmConfigurationCurrent | null>(null); const [ruleSets, setRuleSets] = useState<AlarmRuleSetRevision[]>([]); const [entities, setEntities] = useState<EntityInstance[]>([])
  const [migrationInstallationId, setMigrationInstallationId] = useState(''); const [migrationCandidates, setMigrationCandidates] = useState<LegacyAlarmMigrationCandidate[]>([]); const [scope, setScope] = useState<AlarmEntityScope>(EMPTY_SCOPE)
  const [selectedRuleSetKey, setSelectedRuleSetKey] = useState(''); const [rules, setRules] = useState<AlarmRule[]>([defaultRule()]); const [newRuleSetName, setNewRuleSetName] = useState('现场告警规则'); const [plan, setPlan] = useState<AlarmConfigurationPlan | null>(null)
  const [migrationSelections, setMigrationSelections] = useState<Record<string, string>>({}); const [loading, setLoading] = useState(true); const [planning, setPlanning] = useState(false); const [applying, setApplying] = useState(false); const [migrating, setMigrating] = useState(false)
  const [error, setError] = useState(''); const [success, setSuccess] = useState(''); const [planStale, setPlanStale] = useState(false); const [idempotencyKey, setIdempotencyKey] = useState<string | null>(null)
  const [acceptanceProgress, setAcceptanceProgress] = useState<AlarmConfigurationAcceptanceProgress | null>(null); const [acceptanceReport, setAcceptanceReport] = useState<AlarmConfigurationAcceptanceReport | null>(null); const [acceptanceLoading, setAcceptanceLoading] = useState(false); const [acceptanceRunning, setAcceptanceRunning] = useState(false); const [acceptanceIdempotencyKey, setAcceptanceIdempotencyKey] = useState<string | null>(null); const [acceptanceError, setAcceptanceError] = useState('')
  const selectedRevisionRef = useRef<AlarmRuleSetRevision | null>(null); const initialContextRef = useRef(readWorkspaceContext()); const restoredPlanRef = useRef(''); const acceptanceApplicationRef = useRef('')
  const selectedRuleSet = ruleSets.find((item) => revisionKey(item) === selectedRuleSetKey) || null; const entityNames = useMemo(() => new Map(entities.map((entity) => [entity.id, `${entity.device_display_name} / ${entity.display_name}`])), [entities]); const scopeHasValue = scope.entity_instance_ids.length + scope.device_instance_ids.length + scope.entity_definition_ids.length > 0
  const clearPlanEvidence = () => { setPlan(null); setIdempotencyKey(null); clearWorkspaceContext(); initialContextRef.current = { plan: null, apply: null } }
  const resetPlan = () => { clearPlanEvidence(); setPlanStale(false) }
  const markPlanStale = () => { clearPlanEvidence(); setPlanStale(true) }
  const clearKnownApplyEvidence = () => { setIdempotencyKey(null); clearApplyContext(); initialContextRef.current = readWorkspaceContext() }
  const load = useCallback(async () => {
    setLoading(true); setError('')
    try {
      const [configuration, revisions, entityResponse, migrations] = await Promise.all([getUnifiedAlarmConfiguration(), fetchAlarmRuleSets(), fetchEntityInstances(), fetchLegacyAlarmMigrationCandidates()])
      const saved = initialContextRef.current.plan ? initialContextRef.current : readWorkspaceContext()
      const restored = saved.plan && revisions.find((item) => revisionKey(item) === saved.plan?.revision)
      const retained = selectedRevisionRef.current && revisions.find((item) => revisionKey(item) === revisionKey(selectedRevisionRef.current!))
      const latest = Array.from(new Map(revisions.map((item) => [item.rule_set_id, item])).values())[0] || null
      const chosen = restored || retained || latest
      selectedRevisionRef.current = chosen; setCurrent(configuration); setRuleSets(revisions); setEntities(entityResponse.items); setMigrationInstallationId(migrations.installation_id); setMigrationCandidates(migrations.items); setMigrationSelections({}); setSelectedRuleSetKey(chosen ? revisionKey(chosen) : ''); if (chosen) setRules(chosen.rules)
    } catch (reason) { setError(alarmWorkspaceError(reason, '无法读取告警配置工作台数据，请检查平台连接与权限后重试。')) } finally { setLoading(false) }
  }, [])
  const refreshAcceptance = useCallback(async () => { setAcceptanceLoading(true); setAcceptanceError(''); try { const progress = await progressOrEmpty(); const applicationId = progress?.application_id || ''; if (acceptanceApplicationRef.current !== applicationId) setAcceptanceReport(null); const saved = readAcceptanceRetry(sessionStorage, actorId, applicationId); if (saved) setAcceptanceIdempotencyKey(saved.idempotencyKey); else setAcceptanceIdempotencyKey(null); acceptanceApplicationRef.current = applicationId; setAcceptanceProgress(progress); setAcceptanceReport(null); if (progress?.report_id) { setAcceptanceReport(await fetchAlarmConfigurationReport(progress.report_id)); clearAcceptanceRetry(sessionStorage); setAcceptanceIdempotencyKey(null) } } catch (reason) { setAcceptanceError(alarmWorkspaceError(reason, '无法刷新验收证据，请稍后重试。')) } finally { setAcceptanceLoading(false) } }, [actorId])
  useEffect(() => { void load() }, [load])
  useEffect(() => { void refreshAcceptance() }, [refreshAcceptance])
  useEffect(() => {
    const context = readWorkspaceContext(); if (!current || !selectedRuleSetKey || !context.plan || context.plan.revision !== selectedRuleSetKey || restoredPlanRef.current === context.plan.id) return
    restoredPlanRef.current = context.plan.id
    void getAlarmConfigurationPlan(context.plan.id).then(async (loaded) => {
      const replay = canReplaySavedApply(context, loaded)
      if (replay && context.apply) {
        setPlan(loaded); setIdempotencyKey(context.apply.key); setApplying(true)
        try { const result = await applyAlarmConfigurationPlan(loaded.id, context.apply.digest, context.apply.key); setSuccess(`已恢复并确认告警配置，站点配置版本已更新至 ${result.site_configuration_version}。`); resetPlan(); await refreshAppliedWorkspace(load, refreshAcceptance) }
        catch (reason) {
          if (stalePlan(reason)) markPlanStale()
          else if (unrecoverableReplay(reason)) resetPlan()
          else if (definitiveApplyFailure(reason)) clearKnownApplyEvidence()
          setError(alarmWorkspaceError(reason, '恢复应用结果失败，请稍后重试。'))
        }
        finally { setApplying(false) }
        return
      }
      if (loaded.status === 'ready' && loaded.base_site_configuration_version === current.site_configuration_version && loaded.digest === context.plan?.digest) { setPlan(loaded); return }
      resetPlan()
    }).catch((reason) => { if (stalePlan(reason)) markPlanStale(); else if (unrecoverableReplay(reason)) resetPlan(); setError(alarmWorkspaceError(reason, '无法恢复此前的配置计划，请刷新后重试。')) })
  }, [current, selectedRuleSetKey, load, refreshAcceptance])
  const chooseRuleSet = (key: string) => { const revision = ruleSets.find((item) => revisionKey(item) === key) || null; selectedRevisionRef.current = revision; setSelectedRuleSetKey(key); if (revision) setRules(revision.rules); resetPlan() }
  const createPlan = async () => {
    if (!migrationInstallationId || !scopeHasValue) { setError('请先选择至少一个配置范围。'); return }; const validation = ruleValidation(rules); if (validation) { setError(validation); return }
    setPlanning(true); setError(''); setSuccess('')
    try { let revision = selectedRuleSet; if (!revision) { revision = await createAlarmRuleSet({ key: `workspace-${Date.now()}`, name: newRuleSetName.trim() || '现场告警规则', rules }); setRuleSets((items) => [...items, revision!]) } else if (!sameRules(rules, revision.rules)) { revision = await createAlarmRuleSetRevision(revision.rule_set_id, rules); setRuleSets((items) => [...items, revision!]) }; selectedRevisionRef.current = revision; setSelectedRuleSetKey(revisionKey(revision)); const created = await createAlarmConfigurationPlan({ installation_id: migrationInstallationId, selection: scope, rule_set_id: revision.rule_set_id, rule_set_revision: revision.revision }); setPlan(created); savePlanContext(created); initialContextRef.current = readWorkspaceContext(); setPlanStale(false); setIdempotencyKey(null) }
    catch (reason) { setError(alarmWorkspaceError(reason, '生成计划失败，请检查当前配置后重试。')) } finally { setPlanning(false) }
  }
  const applyPlan = async () => { if (!plan) return; const key = idempotencyKey || crypto.randomUUID(); saveApplyContext(plan, key); setIdempotencyKey(key); setApplying(true); setError(''); setSuccess(''); try { const result = await applyAlarmConfigurationPlan(plan.id, plan.digest, key); setSuccess(`已应用告警配置，站点配置版本已更新至 ${result.site_configuration_version}。`); resetPlan(); await refreshAppliedWorkspace(load, refreshAcceptance) } catch (reason) { if (stalePlan(reason)) markPlanStale(); else if (unrecoverableReplay(reason)) resetPlan(); else if (definitiveApplyFailure(reason)) clearKnownApplyEvidence(); setError(alarmWorkspaceError(reason, '应用结果无法确认，请使用同一操作重试。')) } finally { setApplying(false) } }
  const runAcceptance = async () => { if (!acceptanceProgress?.ready_to_report || acceptanceProgress.report_id) return; const key = acceptanceIdempotencyKey || crypto.randomUUID(); setAcceptanceIdempotencyKey(key); saveAcceptanceRetry(sessionStorage, { actorId, applicationId: acceptanceProgress.application_id, idempotencyKey: key }); setAcceptanceRunning(true); setAcceptanceError(''); setSuccess(''); try { const created = await runAlarmConfigurationAcceptance(acceptanceProgress.application_id, key); const report = await fetchAlarmConfigurationReport(created.id); setAcceptanceReport(report); setAcceptanceProgress((previous) => previous ? { ...previous, report_id: report.id, report_status: report.status, report_digest: report.digest } : previous); clearAcceptanceRetry(sessionStorage); setAcceptanceIdempotencyKey(null); setSuccess('不可变验收报告已生成并重新读取确认。') } catch (reason) { if (reason instanceof AlarmConfigurationApiError && reason.code === 'ALARM_ACCEPTANCE_APPLICATION_STALE') await refreshAcceptance(); setAcceptanceError(alarmWorkspaceError(reason, '验收报告结果无法确认，请使用同一操作重试。')) } finally { setAcceptanceRunning(false) } }
  const migrate = async () => { const selections = migrationCandidates.filter((candidate) => candidate.blockers.some((blocker) => blocker.code === 'ALARM_MIGRATION_AMBIGUOUS')).map((candidate) => ({ source_kind: candidate.source_kind, source_key: candidate.source_key, entity_instance_id: migrationSelections[`${candidate.source_kind}:${candidate.source_key}`] })).filter((selection) => selection.entity_instance_id); setMigrating(true); setError(''); setSuccess(''); try { await createLegacyAlarmMigrationPlan({ installation_id: migrationInstallationId, selections }); setSuccess('旧告警配置已按明确选择迁移，原始配置保持只读。'); await load() } catch (reason) { setError(alarmWorkspaceError(reason, '旧配置迁移失败，请检查候选后重试。')) } finally { setMigrating(false) } }
  if (loading) return <div className="neu-card p-8 text-center text-sm text-gray-500">正在加载统一告警配置工作台...</div>
  return <div className="space-y-4">
    <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
      <div><h2 className="text-base font-bold text-gray-800">统一告警配置</h2><p className="mt-1 text-xs text-gray-500">面向实施工程师的预览优先工作台。严重度固定为严重、重要、警告、提示。</p></div>
      <div className="flex flex-col gap-2 text-xs text-gray-500 sm:flex-row sm:items-center"><span>当前配置版本 {current?.site_configuration_version ?? '未加载'}</span><span>旧配置候选 {migrationCandidates.length}</span><button type="button" onClick={() => { void load(); void refreshAcceptance() }} className="neu-btn px-3 py-1.5 text-xs" disabled={loading}>刷新</button></div>
    </header>
    {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">{error}</div>}
    {success && <div role="status" className="rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-800">{success}</div>}
    {planStale && !plan && <div role="status" className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">原计划已过期并清除，请人工重新生成变更预览。</div>}
    <section aria-labelledby="current-alarm-definitions" className="neu-card p-4">
      <h3 id="current-alarm-definitions" className="text-sm font-bold text-gray-800">当前告警定义</h3>
      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-2">{current?.definitions.map((definition, index) => <article key={`${definition.entity_display_name}-${definition.rule_name}-${index}`} className="rounded-lg border border-white/70 p-2 text-xs"><strong>{definition.entity_display_name}</strong><p className="mt-1 text-gray-600">{definition.rule_name}，{severityLabel[definition.severity]}，触发 {operatorLabel[definition.trigger.operator]} {formatAlarmConditionValue(definition.trigger.value)}，恢复 {operatorLabel[definition.recovery.operator]} {formatAlarmConditionValue(definition.recovery.value)}</p><p className="mt-1 text-gray-500">{definition.version_description}　{definition.enabled ? '已启用' : '未启用'}　当前</p></article>)}{!current?.definitions.length && <p className="text-xs text-gray-400">当前没有已安装的告警定义。</p>}</div>
    </section>
    <AcceptancePanel progress={acceptanceProgress} report={acceptanceReport} entityNames={entityNames} loading={acceptanceLoading} running={acceptanceRunning} error={acceptanceError} onRefresh={() => void refreshAcceptance()} onGenerate={() => void runAcceptance()} onOpenAlarms={onOpenAlarms} />
    <section aria-labelledby="rule-set-heading" className="neu-card p-4"><div className="grid grid-cols-1 gap-3 md:grid-cols-2"><label className="text-xs font-medium text-gray-700">使用已有规则集<select aria-label="选择已有规则集" value={selectedRuleSetKey} onChange={(event) => chooseRuleSet(event.target.value)} className="neu-input mt-1 block w-full px-3 py-2 text-xs"><option value="">新建规则集</option>{ruleSets.map((item) => <option key={revisionKey(item)} value={revisionKey(item)}>{item.name}（第 {item.revision} 版）</option>)}</select></label>{!selectedRuleSetKey ? <label className="text-xs font-medium text-gray-700">新规则集名称<input value={newRuleSetName} onChange={(event) => setNewRuleSetName(event.target.value)} className="neu-input mt-1 block w-full px-3 py-2 text-xs" maxLength={200} /></label> : <p className="self-end pb-2 text-xs text-gray-500">修改规则后会在生成计划时创建新的修订，不会覆盖既有版本。</p>}</div></section>
    <EntityScopePicker entities={entities} value={scope} onChange={(next) => { setScope(next); resetPlan() }} disabled={planning || applying} />
    <RuleSetEditor rules={rules} onChange={(next) => { setRules(next); resetPlan() }} disabled={planning || applying} />
    <div className="flex justify-end"><button type="button" onClick={() => void createPlan()} disabled={planning || applying || !scopeHasValue} className="w-full rounded-lg bg-[#52c41a] px-4 py-2 text-xs font-semibold text-white shadow-sm hover:bg-[#389e0d] focus:outline-none focus:ring-2 focus:ring-[#52c41a]/60 disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto">{planning ? '正在生成计划...' : '生成变更预览'}</button></div>
    {plan && <PlanPreview plan={plan} entityNames={entityNames} applying={applying} stale={planStale} onApply={() => void applyPlan()} />}
    <LegacyMigrationPanel candidates={migrationCandidates} entities={entities} selections={migrationSelections} onSelectionChange={(sourceKind, sourceKey, entityInstanceId) => setMigrationSelections((previous) => ({ ...previous, [`${sourceKind}:${sourceKey}`]: entityInstanceId }))} onMigrate={() => void migrate()} migrating={migrating} />
  </div>
}
