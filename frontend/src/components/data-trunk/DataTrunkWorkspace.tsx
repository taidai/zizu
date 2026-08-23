import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  DataTrunkApiError,
  DataTrunkResultUnknownError,
  applyPointProcessingPlan,
  connectEntityObservationWS,
  createPointProcessingPlan,
  fetchEntityInstanceHistory,
  fetchEntityInstances,
  fetchNodeDataTrunk,
  fetchPointProcessingAcceptanceState,
  fetchPointProcessingPlan,
  fetchPointProcessingTemplates,
  runEN9PointProcessingAcceptance,
  type EN9AcceptanceReport,
  type EntityInstance,
  type EntityInstanceObservation,
  type Node,
  type NodeDataTrunk,
  type PointProcessingApplication,
  type PointProcessingPlan,
  type PointProcessingTemplate,
} from '../../api/client'
import {
  clearDataTrunkApplyRetry,
  findDataTrunkApplyRetry,
  readDataTrunkApplyRetry,
  saveDataTrunkApplyRetry,
} from './dataTrunkRetryState'
import NodeTrunkOverview from './NodeTrunkOverview'
import PointProcessingPlanPanel from './PointProcessingPlanPanel'
import { DATA_TRUNK_STEPS } from './dataTrunkViewModel'

export default function DataTrunkWorkspace({
  node,
  readOnly,
  actorId,
}: {
  node: Node
  readOnly: boolean
  actorId: string
}) {
  const [trunk, setTrunk] = useState<NodeDataTrunk | null>(null)
  const [templates, setTemplates] = useState<PointProcessingTemplate[]>([])
  const [selectedRevisionId, setSelectedRevisionId] = useState('')
  const [selections, setSelections] = useState<Record<string, string>>({})
  const [plan, setPlan] = useState<PointProcessingPlan | null>(null)
  const [application, setApplication] = useState<PointProcessingApplication | null>(null)
  const [acceptance, setAcceptance] = useState<EN9AcceptanceReport | null>(null)
  const [descriptors, setDescriptors] = useState<Map<string, EntityInstance>>(new Map())
  const [observations, setObservations] = useState<Map<string, EntityInstanceObservation>>(new Map())
  const [histories, setHistories] = useState<Map<string, EntityInstanceObservation[]>>(new Map())
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<'plan' | 'apply' | 'acceptance' | null>(null)
  const [error, setError] = useState('')
  const [resultUnknown, setResultUnknown] = useState(false)

  const loadRuntime = useCallback(async () => {
    const [nextTrunk, catalog] = await Promise.all([
      fetchNodeDataTrunk(node.id),
      fetchEntityInstances(),
    ])
    const descriptorMap = new Map(catalog.items.map((item) => [item.id, item]))
    const historyEntries = await Promise.all(nextTrunk.l2.map(async (item) => {
      try {
        const items = await fetchEntityInstanceHistory(item.entity_instance_id)
        return [item.entity_instance_id, items] as const
      } catch {
        return [item.entity_instance_id, [] as EntityInstanceObservation[]] as const
      }
    }))
    const nextHistories = new Map(historyEntries)
    const nextObservations = new Map<string, EntityInstanceObservation>()
    for (const [entityId, items] of historyEntries) {
      if (items.length > 0) nextObservations.set(entityId, items[items.length - 1])
    }
    setTrunk(nextTrunk)
    setDescriptors(descriptorMap)
    setHistories(nextHistories)
    setObservations(nextObservations)
    return nextTrunk
  }, [node.id])

  const loadWorkspace = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const nextTrunk = await loadRuntime()
      if (!readOnly) {
        const [nextTemplates, acceptanceState] = await Promise.all([
          fetchPointProcessingTemplates((node.node_type || 'PCS').toUpperCase()),
          fetchPointProcessingAcceptanceState(node.id),
        ])
        setTemplates(nextTemplates)
        setApplication(acceptanceState.application)
        setAcceptance(acceptanceState.latest_report)
        setSelectedRevisionId((current) => current || nextTrunk.l1_summary.revision_id || nextTemplates[0]?.revision_id || '')
        const retry = findDataTrunkApplyRetry(sessionStorage, actorId, node.id)
        if (retry) {
          try {
            const restoredPlan = await fetchPointProcessingPlan(retry.planId)
            if (readDataTrunkApplyRetry(sessionStorage, {
              actorId,
              nodeId: node.id,
              planId: restoredPlan.id,
              planDigest: restoredPlan.digest,
            })) {
              setPlan(restoredPlan)
              setSelectedRevisionId(restoredPlan.template_revision_id)
              setResultUnknown(true)
            }
          } catch {
            clearDataTrunkApplyRetry(sessionStorage)
          }
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '读取节点数据主干失败')
    } finally {
      setLoading(false)
    }
  }, [actorId, loadRuntime, node.id, node.node_type, readOnly])

  useEffect(() => {
    setPlan(null)
    setApplication(null)
    setAcceptance(null)
    setSelectedRevisionId('')
    setSelections({})
    setResultUnknown(false)
    void loadWorkspace()
  }, [loadWorkspace])

  const l2Ids = useMemo(() => trunk?.l2.map((item) => item.entity_instance_id) || [], [trunk])
  useEffect(() => connectEntityObservationWS(
    l2Ids,
    (observation) => {
      setObservations((current) => new Map(current).set(observation.entity_instance_id, observation))
      setHistories((current) => {
        const next = new Map(current)
        const items = [...(next.get(observation.entity_instance_id) || []), observation]
        next.set(observation.entity_instance_id, items.slice(-120))
        return next
      })
    },
    async () => { await loadRuntime() },
    application?.id,
  ), [l2Ids.join('|'), loadRuntime, application?.id])

  const selectedTemplate = templates.find((item) => item.revision_id === selectedRevisionId) || null
  const installedTemplate = templates.find((item) => item.revision_id === trunk?.l1_summary.revision_id) || null

  useEffect(() => {
    if (!selectedTemplate || !trunk) return
    const initial: Record<string, string> = {}
    for (const input of selectedTemplate.inputs) {
      const current = trunk.l1_summary.input_bindings?.[input.input_id]
      if (current && trunk.l0.some((source) => source.source_id === current)) {
        initial[input.input_id] = current
        continue
      }
      const keys = new Set([input.source_key, ...input.aliases].map((value) => value.toLowerCase()))
      const candidates = trunk.l0.filter((source) => (
        keys.has(source.source_key.toLowerCase())
        && source.data_type === input.data_type
        && (source.unit || null) === (input.unit || null)
      ))
      if (candidates.length === 1) initial[input.input_id] = candidates[0].source_id
    }
    setSelections(initial)
    setPlan(null)
    setResultUnknown(false)
  }, [selectedRevisionId, trunk?.node_id])

  const handlePlan = async () => {
    if (!selectedTemplate) return
    setBusy('plan')
    setError('')
    clearDataTrunkApplyRetry(sessionStorage)
    setResultUnknown(false)
    try {
      setPlan(await createPointProcessingPlan(node.id, {
        template_revision_id: selectedTemplate.revision_id,
        input_selections: Object.fromEntries(Object.entries(selections).filter(([, value]) => value)),
      }))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '生成计划失败')
    } finally {
      setBusy(null)
    }
  }

  const handleApply = async () => {
    if (!plan) return
    setAcceptance(null)
    setBusy('apply')
    setError('')
    const identity = { actorId, nodeId: node.id, planId: plan.id, planDigest: plan.digest }
    const existing = readDataTrunkApplyRetry(sessionStorage, identity)
    const retry = existing || { ...identity, idempotencyKey: crypto.randomUUID() }
    saveDataTrunkApplyRetry(sessionStorage, retry)
    try {
      const result = await applyPointProcessingPlan(plan.id, plan.digest, retry.idempotencyKey)
      setApplication(result)
      setPlan({ ...plan, status: 'applied' })
      clearDataTrunkApplyRetry(sessionStorage)
      setResultUnknown(false)
      await loadRuntime()
    } catch (reason) {
      const shouldKeep = reason instanceof DataTrunkResultUnknownError
        || reason instanceof TypeError
        || (reason instanceof DataTrunkApiError && reason.retryable)
      if (!shouldKeep) clearDataTrunkApplyRetry(sessionStorage)
      setResultUnknown(shouldKeep)
      setError(reason instanceof Error ? reason.message : '应用点位加工失败')
    } finally {
      setBusy(null)
    }
  }

  const handleAcceptance = async () => {
    if (!application) return
    setBusy('acceptance')
    setError('')
    try {
      setAcceptance(await runEN9PointProcessingAcceptance(application.id, 1800))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '机器验收失败')
    } finally {
      setBusy(null)
    }
  }

  const currentAcceptance = acceptance?.application_id === application?.id
    ? acceptance
    : null
  const completedStage = currentAcceptance?.passed
    ? 4
    : application || plan?.status === 'applied'
      ? 3
      : plan
        ? 2
        : selectedTemplate
          ? 1
          : trunk?.l1_summary.installed ? 3 : 0

  if (loading) {
    return <div className="neu-card p-6 text-sm text-gray-500">正在读取节点数据主干...</div>
  }
  if (!trunk) {
    return (
      <div className="neu-card p-6">
        <p className="text-sm font-semibold text-gray-800">节点数据主干不可用</p>
        <p className="mt-1 text-xs text-red-600">{error || '请检查节点安装和平台连接。'}</p>
        <button type="button" onClick={() => void loadWorkspace()} className="neu-btn mt-4 px-3 py-2 text-xs text-blue-700">重新读取</button>
      </div>
    )
  }

  return (
    <div className="space-y-3 pb-3">
      <header className="rounded-xl border border-gray-200 bg-white/55 p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h2 className="text-base font-semibold text-gray-900">{node.name} 数据主干</h2>
            <p className="mt-1 text-xs text-gray-500">原始点位经过点位加工形成稳定全局实体，上层功能不再直接依赖品牌地址。</p>
          </div>
          <div className="grid grid-cols-2 gap-1 sm:grid-cols-5">
            {DATA_TRUNK_STEPS.map((step, index) => (
              <div key={step.key} className={`rounded-lg border px-2 py-2 text-center text-[10px] font-medium ${index <= completedStage ? 'border-blue-200 bg-blue-50 text-blue-800' : 'border-gray-200 bg-gray-50 text-gray-500'}`}>
                {step.label}
              </div>
            ))}
          </div>
        </div>
      </header>

      {error && (
        <div role="alert" className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700">
          {error}
        </div>
      )}

      <NodeTrunkOverview
        trunk={trunk}
        installedTemplate={installedTemplate}
        descriptors={descriptors}
        observations={observations}
        histories={histories}
        readOnly={readOnly}
      />

      {!readOnly && (
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_360px]">
          <div className="rounded-xl border border-gray-200 bg-white/45 p-4">
            <h3 className="text-sm font-semibold text-gray-900">交付证据</h3>
            <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
              <div className="border-l-2 border-blue-500 pl-3"><div className="text-[10px] text-gray-500">已安装输出</div><div className="mt-1 text-sm font-semibold text-gray-900">{trunk.l1_summary.output_count} 个全局实体</div></div>
              <div className="border-l-2 border-blue-500 pl-3"><div className="text-[10px] text-gray-500">站点配置版本</div><div className="mt-1 text-sm font-semibold text-gray-900">{application?.site_configuration_version ?? observations.values().next().value?.site_configuration_version ?? '等待安装'}</div></div>
              <div className="border-l-2 border-blue-500 pl-3">
                <div className="text-[10px] text-gray-500">运行验收</div>
                <div className="mt-1 text-sm font-semibold text-gray-900">{currentAcceptance ? (currentAcceptance.passed ? '机器验收通过' : '机器验收未通过') : application ? (observations.size >= 3 ? '三实体在线，待机器报告' : '等待三实体实时值') : '应用后生成'}</div>
                {application && !currentAcceptance?.passed && (
                  <button type="button" disabled={busy !== null} onClick={() => void handleAcceptance()} className="neu-btn mt-2 px-2 py-1 text-[10px] text-blue-700 disabled:opacity-50">
                    {busy === 'acceptance' ? '验收中...' : currentAcceptance ? '重新运行机器验收' : '运行机器验收'}
                  </button>
                )}
                {currentAcceptance && (
                  <div className="mt-2 space-y-1 text-[10px] text-gray-500">
                    <div>报告 {currentAcceptance.id.slice(0, 8)} · 摘要 {currentAcceptance.digest.slice(0, 12)}</div>
                    {currentAcceptance.checks.map((check) => (
                      <div key={check.code} className={`rounded border px-2 py-1 ${check.passed ? 'border-emerald-100 text-emerald-700' : 'border-red-100 bg-red-50 text-red-700'}`}>
                        <div>{check.passed ? '通过' : '未通过'} · {check.code}</div>
                        <div className="mt-0.5 break-all text-gray-500">{Object.entries(check.evidence).map(([key, value]) => `${key}=${String(value)}`).join(' · ')}</div>
                        {!check.passed && <div className="mt-0.5">建议：检查该项证据后修复，再重新运行验收。</div>}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
          <PointProcessingPlanPanel
            trunk={trunk}
            templates={templates}
            selectedTemplate={selectedTemplate}
            selections={selections}
            plan={plan}
            busy={busy}
            resultUnknown={resultUnknown}
            onTemplateChange={setSelectedRevisionId}
            onSelectionChange={(inputId, sourceId) => setSelections((current) => ({ ...current, [inputId]: sourceId }))}
            onPlan={() => void handlePlan()}
            onApply={() => void handleApply()}
          />
        </div>
      )}
    </div>
  )
}
