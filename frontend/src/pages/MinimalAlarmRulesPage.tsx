import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  applyAlarmConfigurationPlan,
  createAlarmConfigurationPlan,
  createAlarmRuleSet,
  createAlarmRuleSetRevision,
  deleteAlarmRuleSet,
  fetchAlarmHttpNotificationOptions,
  fetchAlarmRuleGroups,
  fetchAlarmRuleSets,
  fetchEntityInstances,
  trialAlarmRule,
  type AlarmRule,
  type AlarmHttpNotificationOption,
  type AlarmRuleGroup,
  type AlarmRuleSetRevision,
  type AlarmSeverity,
  type EntityInstance,
} from '../api/client'
import {
  compileFaultCodeRules,
  canDeleteAlarmRuleGroup,
  defaultAlarmDraft,
  describeAlarmDraft,
  describeAlarmTrialResult,
  parseFaultCodePaste,
  prepareAlarmRuleEdit,
  prepareAlarmTrialInput,
  type AlarmDraftDataType,
} from '../components/alarm-center/alarmCenterModel'

const revisionKey = (item: AlarmRuleSetRevision) => `${item.rule_set_id}:${item.revision}`
const severityLabel: Record<AlarmSeverity, string> = { CRITICAL: '紧急', MAJOR: '重要', WARNING: '警告', INFO: '提示' }

function compatible(entity: EntityInstance, type: AlarmDraftDataType) {
  const dataType = entity.data_type.toUpperCase()
  if (type === 'CODE_SET') return dataType === 'CODE_SET'
  if (type === 'NUMBER') return ['FLOAT', 'INT', 'NUMBER', 'NUMERIC', 'DOUBLE', 'DECIMAL'].includes(dataType)
  return ['BOOL', 'BOOLEAN', 'STRING', 'STATE', 'ENUM'].includes(dataType)
}

export default function MinimalAlarmRulesPage() {
  const [groups, setGroups] = useState<AlarmRuleGroup[]>([])
  const [revisions, setRevisions] = useState<AlarmRuleSetRevision[]>([])
  const [entities, setEntities] = useState<EntityInstance[]>([])
  const [httpNotifications, setHttpNotifications] = useState<AlarmHttpNotificationOption[]>([])
  const [notificationsLoading, setNotificationsLoading] = useState(true)
  const [notificationsError, setNotificationsError] = useState('')
  const notificationsRequest = useRef<AbortController | null>(null)
  const [selectedRevision, setSelectedRevision] = useState<AlarmRuleSetRevision | null>(null)
  const [selectedEntities, setSelectedEntities] = useState<string[]>([])
  const [dataType, setDataType] = useState<AlarmDraftDataType>('NUMBER')
  const [name, setName] = useState('现场告警规则')
  const [rules, setRules] = useState<AlarmRule[]>([defaultAlarmDraft('NUMBER')])
  const [faultPaste, setFaultPaste] = useState('')
  const [trialValue, setTrialValue] = useState('0')
  const [trialText, setTrialText] = useState('')
  const [plan, setPlan] = useState<Awaited<ReturnType<typeof createAlarmConfigurationPlan>> | null>(null)
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setBusy('load'); setError('')
    try {
      const [nextGroups, nextRevisions, response] = await Promise.all([
        fetchAlarmRuleGroups(), fetchAlarmRuleSets(), fetchEntityInstances(),
      ])
      setGroups(nextGroups); setRevisions(nextRevisions); setEntities(response.items)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法读取告警规则。') }
    finally { setBusy('') }
  }, [])
  useEffect(() => { void load() }, [load])

  const loadNotifications = useCallback(async () => {
    notificationsRequest.current?.abort()
    const controller = new AbortController()
    notificationsRequest.current = controller
    setNotificationsLoading(true); setNotificationsError('')
    try {
      const options = await fetchAlarmHttpNotificationOptions(controller.signal)
      if (!controller.signal.aborted) setHttpNotifications(options)
    } catch (reason) {
      if (!controller.signal.aborted) setNotificationsError(reason instanceof Error ? reason.message : '请求未完成')
    } finally {
      if (!controller.signal.aborted) setNotificationsLoading(false)
    }
  }, [])
  useEffect(() => {
    void loadNotifications()
    return () => notificationsRequest.current?.abort()
  }, [loadNotifications])

  const selectableEntities = useMemo(() => entities.filter((entity) => compatible(entity, dataType)), [entities, dataType])
  const firstRule = rules[0]
  const selectedHttpNotificationId = firstRule?.http_notification_config_id || ''
  const availableHttpNotifications = useMemo(
    () => httpNotifications.filter((config) => config.status === 'available'),
    [httpNotifications],
  )
  const selectedEntity = useMemo(
    () => entities.find((item) => item.id === selectedEntities[0]),
    [entities, selectedEntities],
  )
  const selectedEntityIsBoolean = ['BOOL', 'BOOLEAN'].includes((selectedEntity?.data_type || '').toUpperCase())
  const trialInput = useMemo(
    () => prepareAlarmTrialInput(dataType, selectedEntity?.data_type, trialValue),
    [dataType, selectedEntity?.data_type, trialValue],
  )
  const resetResult = () => { setTrialText(''); setPlan(null); setMessage('') }
  const patchRule = (patch: Partial<AlarmRule>) => {
    setRules((items) => [{ ...items[0], ...patch }, ...items.slice(1)]); resetResult()
  }
  const changeType = (type: AlarmDraftDataType) => {
    setDataType(type); setRules([defaultAlarmDraft(type)]); setSelectedEntities([]); setSelectedRevision(null); resetResult()
  }

  const editGroup = (group: AlarmRuleGroup, copy: boolean) => {
    const revision = revisions.find((item) => item.rule_set_id === group.rule_set_id && item.revision === group.last_non_empty_revision)
    if (!revision) return
    const operator = revision.rules[0]?.trigger.operator
    const entityDataTypes = group.entity_instance_ids.map((id) => entities.find((item) => item.id === id)?.data_type)
    if (entityDataTypes.some((item) => !item)) { setError('找不到规则组绑定实体的类型信息，请刷新后重试。'); return }
    const prepared = prepareAlarmRuleEdit(revision.rules, entityDataTypes as string[])
    if (!prepared.ready) { setError(prepared.message); return }
    setError('')
    setDataType(operator === 'contains' ? 'CODE_SET' : ['gt', 'gte', 'lt', 'lte'].includes(operator || '') ? 'NUMBER' : 'STATE')
    setRules(prepared.rules)
    if (prepared.booleanEntity) setTrialValue('false')
    setSelectedEntities(group.entity_instance_ids); setName(copy ? `${group.name} 副本` : group.name)
    setSelectedRevision(copy ? null : revision); setFaultPaste(''); resetResult()
  }

  const selectHttpNotification = (configId: string) => {
    setRules((current) => current.map((rule) => ({
      ...rule,
      http_notification_config_id: configId || null,
    })))
    resetResult()
  }

  const runTrial = async () => {
    if (!firstRule || !selectedEntity || !trialInput.ready) { setError(trialInput.message); return }
    setBusy('trial'); setError('')
    try {
      const result = await trialAlarmRule({ entity_instance_id: selectedEntity.id, rule: firstRule, value: trialInput.value, quality: 192 })
      setTrialText(describeAlarmTrialResult(result, firstRule, trialInput.value, {
        displayName: `${selectedEntity.node_display_name} / ${selectedEntity.display_name}`,
        unit: selectedEntity.unit,
      }))
    } catch (reason) { setError(reason instanceof Error ? reason.message : '试算失败。') }
    finally { setBusy('') }
  }

  const preview = async () => {
    if (!selectedEntities.length || !rules.length) { setError('请选择至少一个兼容的 L2 实体并填写规则。'); return }
    if (!trialText) { setError('请先试算，确认规则含义。'); return }
    setBusy('plan'); setError('')
    try {
      let revision = selectedRevision
      if (!revision) revision = await createAlarmRuleSet({ key: `alarm-${Date.now()}`, name: name.trim() || '现场告警规则', rules })
      else if (JSON.stringify(revision.rules) !== JSON.stringify(rules)) revision = await createAlarmRuleSetRevision(revision.rule_set_id, rules)
      setSelectedRevision(revision)
      setPlan(await createAlarmConfigurationPlan({
        selection: { entity_instance_ids: selectedEntities, node_ids: [], entity_definition_ids: [] },
        rule_set_id: revision.rule_set_id, rule_set_revision: revision.revision,
      }))
    } catch (reason) { setError(reason instanceof Error ? reason.message : '生成预览失败。') }
    finally { setBusy('') }
  }

  const publish = async () => {
    if (!plan) return
    setBusy('apply'); setError('')
    try {
      const result = await applyAlarmConfigurationPlan(plan.id, plan.digest, crypto.randomUUID())
      setMessage(`已发布，统一配置版本 ${result.configuration_revision}。`); setPlan(null); await load()
    } catch (reason) { setError(reason instanceof Error ? reason.message : '发布结果不明确，请先刷新检查，不要重复点击。') }
    finally { setBusy('') }
  }

  const toggleGroup = async (group: AlarmRuleGroup) => {
    if (!group.entity_instance_ids.length) return
    setBusy(`toggle:${group.rule_set_id}`); setError('')
    try {
      const enabled = group.enabled_entity_instance_ids.length > 0
      let revision: AlarmRuleSetRevision | undefined
      if (enabled) revision = await createAlarmRuleSetRevision(group.rule_set_id, [])
      else revision = revisions.find((item) => item.rule_set_id === group.rule_set_id && item.revision === group.last_published_revision)
      if (!revision) throw new Error('没有可直接恢复的正式配置，请编辑、试算并明确发布；不会自动启用草稿。')
      const nextPlan = await createAlarmConfigurationPlan({
        selection: { entity_instance_ids: group.entity_instance_ids, node_ids: [], entity_definition_ids: [] },
        rule_set_id: revision.rule_set_id, rule_set_revision: revision.revision,
      })
      await applyAlarmConfigurationPlan(nextPlan.id, nextPlan.digest, crypto.randomUUID())
      await load()
    } catch (reason) { setError(reason instanceof Error ? reason.message : '启停失败，请刷新确认实际状态。') }
    finally { setBusy('') }
  }

  const deleteGroup = async (group: AlarmRuleGroup) => {
    if (!canDeleteAlarmRuleGroup(group)) {
      setError('请先停用该规则，再删除。')
      return
    }
    if (!window.confirm(`确定删除“${group.name}”吗？\n历史修订和告警证据仍会保留。`)) return
    setBusy(`delete:${group.rule_set_id}`); setError(''); setMessage('')
    try {
      await deleteAlarmRuleSet(group.rule_set_id)
      if (selectedRevision?.rule_set_id === group.rule_set_id) setSelectedRevision(null)
      await load()
      setMessage('规则已删除，历史证据仍保留。')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '删除规则失败。') }
    finally { setBusy('') }
  }

  return <div className="space-y-4">
    {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">{error}</div>}
    {message && <div className="rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-800">{message}</div>}
    <section className="neu-card p-4">
      <div className="flex items-center justify-between"><div><h3 className="text-sm font-bold text-gray-800">已配置规则</h3><p className="mt-1 text-xs text-gray-500">启停也通过正式发布链，数据库提交后才改变。</p></div><button onClick={() => { setSelectedRevision(null); setName('现场告警规则'); changeType('NUMBER') }} className="rounded-lg bg-[#52c41a] px-3 py-2 text-xs font-semibold text-white">新建规则</button></div>
      <p className="mt-2 text-xs text-gray-500">启用只恢复正式配置，不发布草稿。同一次未恢复告警不会重复发送 HTTP 通知；启停、确认不补发消息，旧告警仍按发生时的条件等待恢复。</p>
      <div className="mt-3 space-y-2">{groups.map((group) => {
        const enabled = group.enabled_entity_instance_ids.length > 0
        const formal = revisions.find((item) => item.rule_set_id === group.rule_set_id && item.revision === group.last_published_revision)
        const draft = revisions.find((item) => item.rule_set_id === group.rule_set_id && item.revision === group.last_non_empty_revision)
        const hasDraft = formal && draft && draft.revision > formal.revision && draft.digest !== formal.digest
        return <div key={group.rule_set_id} className="rounded-lg border border-white/70 p-3 text-xs">
          <div className="flex flex-wrap items-center gap-3">
            <strong className="min-w-32">{group.name}</strong><span>{group.device_count} 台设备</span>
            <span>{group.rule_count} 条 · {group.highest_severity ? severityLabel[group.highest_severity] : '空'}</span>
            <span className={enabled ? 'text-green-700' : 'text-gray-400'}>{enabled ? '已启用' : '已停用'}</span>
            <div className="ml-auto flex gap-2">
              <button onClick={() => editGroup(group, false)} className="text-[#287c12]">编辑</button>
              <button onClick={() => editGroup(group, true)} className="text-gray-600">复制</button>
              <button disabled={!!busy || (!enabled && (!formal || !group.entity_instance_ids.length))} onClick={() => void toggleGroup(group)} className={`${enabled ? 'text-red-600' : 'text-green-700'} disabled:opacity-40`}>{enabled ? '停用' : '启用'}</button>
              <button disabled={!!busy || !canDeleteAlarmRuleGroup(group)} onClick={() => void deleteGroup(group)} className="text-red-600 disabled:opacity-30">删除</button>
            </div>
          </div>
          {formal ? <div data-testid={`alarm-group-formal-${group.rule_set_id}`} className="mt-2 space-y-1 text-gray-600">
            <p>{enabled ? '当前生效' : '再次启用将恢复'}：正式修订 {formal.revision}</p>
            {formal.rules.map((rule) => <p key={rule.id}>{describeAlarmDraft(rule, { displayName: rule.name, unit: rule.unit || null })}</p>)}
          </div> : <p className="mt-2 text-amber-700">没有可直接恢复的正式配置：尚未发布、记录不完整或设备版本不一致。请编辑、试算并明确发布。</p>}
          {hasDraft && <p className="mt-2 text-amber-700">另有未发布草稿（修订 {draft.revision}），编辑时可继续；启停不会发布它。</p>}
        </div>
      })}{!groups.length && <p className="py-4 text-xs text-gray-400">暂无规则。</p>}</div>
    </section>
    <section className="neu-card p-4 space-y-4">
      <h3 className="text-sm font-bold text-gray-800">1. 选择实体</h3>
      <div className="flex gap-2">{(['NUMBER', 'STATE', 'CODE_SET'] as AlarmDraftDataType[]).map((type) => <button key={type} onClick={() => changeType(type)} className={`rounded px-3 py-1.5 text-xs ${dataType === type ? 'bg-[#52c41a] text-white' : 'bg-white/50 text-gray-600'}`}>{type === 'NUMBER' ? '数值' : type === 'STATE' ? '状态' : '多故障码'}</button>)}</div>
      <div className="grid gap-2 md:grid-cols-2">{selectableEntities.map((entity) => <label key={entity.id} className="flex items-center gap-2 rounded border border-white/70 p-2 text-xs"><input type="checkbox" checked={selectedEntities.includes(entity.id)} onChange={(event) => {
        const selectingFirst = event.target.checked && selectedEntities.length === 0
        if (event.target.checked && !selectingFirst) {
          const selectedTypes = selectedEntities.map((id) => entities.find((item) => item.id === id)?.data_type).filter((item): item is string => !!item)
          const prepared = prepareAlarmRuleEdit([], [...selectedTypes, entity.data_type])
          if (!prepared.ready) { setError(prepared.message); return }
        }
        setSelectedEntities((items) => event.target.checked ? [...items, entity.id] : items.filter((id) => id !== entity.id))
        if (selectingFirst && dataType === 'STATE' && ['BOOL', 'BOOLEAN'].includes(entity.data_type.toUpperCase())) {
          setRules([defaultAlarmDraft('STATE', entity.data_type)])
          setTrialValue('false')
        }
        setError(''); resetResult()
      }} /><span><strong>{entity.node_display_name}</strong> / {entity.display_name} <span className="text-gray-400">{entity.unit || entity.data_type}</span></span></label>)}</div>
      <h3 className="text-sm font-bold text-gray-800">2. 设置规则</h3>
      <label className="block text-xs">规则名称<input value={name} onChange={(event) => setName(event.target.value)} className="neu-input mt-1 w-full px-3 py-2" /></label>
      {dataType === 'CODE_SET' ? <div><textarea value={faultPaste} onChange={(event) => setFaultPaste(event.target.value)} rows={5} placeholder={'E30\t压缩机故障\tMAJOR\nE42\t直流母线过压\tCRITICAL'} className="neu-input w-full px-3 py-2 font-mono text-xs"/><button onClick={() => { try { setRules(compileFaultCodeRules(parseFaultCodePaste(faultPaste)).map((rule) => ({ ...rule, http_notification_config_id: selectedHttpNotificationId || null }))); resetResult(); setError('') } catch (reason) { setError(reason instanceof Error ? reason.message : '故障码格式错误。') } }} className="mt-2 rounded bg-white/70 px-3 py-1.5 text-xs">解析故障码</button><p className="mt-1 text-xs text-gray-500">已解析 {rules.length} 条规则</p></div> : firstRule && <div className="grid gap-3 md:grid-cols-4"><label className="text-xs">故障名称<input value={firstRule.name} onChange={(event) => patchRule({ name: event.target.value })} className="neu-input mt-1 w-full px-2 py-2" /></label><label className="text-xs">等级<select value={firstRule.severity} onChange={(event) => patchRule({ severity: event.target.value as AlarmSeverity })} className="neu-input mt-1 w-full px-2 py-2"><option value="CRITICAL">紧急</option><option value="MAJOR">重要</option><option value="WARNING">警告</option><option value="INFO">提示</option></select></label><label className="text-xs">触发值{selectedEntityIsBoolean ? <select value={String(firstRule.trigger.value)} onChange={(event) => patchRule({ trigger: { ...firstRule.trigger, value: event.target.value === 'true' } })} className="neu-input mt-1 w-full px-2 py-2"><option value="true">true（故障）</option><option value="false">false（正常）</option></select> : <input value={String(firstRule.trigger.value)} onChange={(event) => patchRule({ trigger: { ...firstRule.trigger, value: dataType === 'NUMBER' ? Number(event.target.value) : event.target.value } })} className="neu-input mt-1 w-full px-2 py-2" />}</label><label className="text-xs">恢复值{selectedEntityIsBoolean ? <select value={String(firstRule.recovery.value)} onChange={(event) => patchRule({ recovery: { ...firstRule.recovery, value: event.target.value === 'true' } })} className="neu-input mt-1 w-full px-2 py-2"><option value="false">false（正常）</option><option value="true">true（故障）</option></select> : <input value={String(firstRule.recovery.value)} onChange={(event) => patchRule({ recovery: { ...firstRule.recovery, value: dataType === 'NUMBER' ? Number(event.target.value) : event.target.value } })} className="neu-input mt-1 w-full px-2 py-2" />}</label></div>}
      <div className="max-w-xl text-xs">
        <div className="flex items-center justify-between gap-2">
          <label htmlFor="alarm-http-notification">HTTP 通知（可选）</label>
          <button type="button" onClick={() => void loadNotifications()} disabled={notificationsLoading} className="rounded bg-white/70 px-3 py-1.5 text-[#287c12] disabled:opacity-40">刷新通知选项</button>
        </div>
        <select id="alarm-http-notification" value={selectedHttpNotificationId} disabled={notificationsLoading || !!notificationsError} aria-describedby="alarm-http-notification-help" onChange={(event) => selectHttpNotification(event.target.value)} className="neu-input mt-1 w-full px-3 py-2">
          <option value="">不发送 HTTP 通知</option>
          {selectedHttpNotificationId && !availableHttpNotifications.some((config) => config.id === selectedHttpNotificationId) && <option value={selectedHttpNotificationId}>已绑定但当前不可用，请到系统工具检查</option>}
          {availableHttpNotifications.map((config) => <option key={config.id} value={config.id}>{config.name}</option>)}
        </select>
        <div id="alarm-http-notification-help" className="mt-2 space-y-1 text-[11px] text-gray-600">
          {notificationsLoading ? <p role="status">正在读取 HTTP 通知选项…</p> : notificationsError ? <p role="alert" className="text-red-700">HTTP 通知选项读取失败：{notificationsError}。请点击“刷新通知选项”重试；原有选择保留。</p> : <>
            {!httpNotifications.length && <p>尚未配置 HTTP 通知。</p>}
            {httpNotifications.filter((config) => config.status !== 'available').map((config) => <p key={config.id}>{config.name}：{config.status === 'disabled' ? '已停用' : '需测试并启用'}</p>)}
            {(!httpNotifications.length || httpNotifications.some((config) => config.status !== 'available') || (selectedHttpNotificationId && !availableHttpNotifications.some((config) => config.id === selectedHttpNotificationId))) && <p>请在“系统工具 → HTTP 通知”新建或检查配置，完成测试并启用后，点击“刷新通知选项”。实施工程师请联系管理员。</p>}
          </>}
          <p>仅已测试并启用的通知可选；规则发生和恢复时都会发送。</p>
        </div>
      </div>
      {firstRule && selectedEntities[0] && <p className="rounded bg-white/50 p-3 text-xs text-gray-700">{describeAlarmDraft(firstRule, { displayName: entities.find((item) => item.id === selectedEntities[0])?.display_name || '所选实体', unit: entities.find((item) => item.id === selectedEntities[0])?.unit || null })}</p>}
      <h3 className="text-sm font-bold text-gray-800">3. 试算并发布</h3>
      {selectedEntity && <p className="text-xs text-gray-600">本次试算对象：<strong>{selectedEntity.node_display_name} / {selectedEntity.display_name}</strong>{selectedEntities.length > 1 ? `（已批量选择 ${selectedEntities.length} 个实体，试算以此实体为准）` : ''}</p>}
      <div className="flex flex-wrap items-end gap-2"><label className="text-xs">试算值{selectedEntityIsBoolean ? <select value={trialValue} onChange={(event) => { setTrialValue(event.target.value); setTrialText('') }} className="neu-input mt-1 block px-3 py-2"><option value="false">false（正常）</option><option value="true">true（故障）</option></select> : <input value={trialValue} onChange={(event) => { setTrialValue(event.target.value); setTrialText('') }} placeholder={dataType === 'CODE_SET' ? 'E30,E42' : '当前值'} className="neu-input mt-1 block px-3 py-2" />}</label><button onClick={() => void runTrial()} disabled={!!busy || !trialInput.ready} className="rounded bg-white/70 px-3 py-2 text-xs disabled:opacity-40">试算</button><button onClick={() => void preview()} disabled={!!busy || !trialText} className="rounded bg-[#52c41a] px-3 py-2 text-xs font-semibold text-white disabled:opacity-40">生成发布预览</button></div>
      {!trialInput.ready && <p role="status" className="rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">{trialInput.message}</p>}
      {trialText && <p className="rounded border border-green-200 bg-green-50 p-3 text-xs text-green-800">{trialText}</p>}
      {plan && <div className="rounded border border-white/70 p-3 text-xs"><p>将影响 {selectedEntities.length} 个实体，共 {plan.items.length} 项变更。</p><button onClick={() => void publish()} disabled={!!busy || plan.status !== 'ready'} className="mt-3 rounded bg-[#52c41a] px-3 py-2 font-semibold text-white disabled:opacity-40">确认发布</button></div>}
    </section>
  </div>
}
