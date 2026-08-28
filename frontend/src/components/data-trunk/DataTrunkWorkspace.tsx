import { useCallback, useEffect, useState } from 'react'
import {
  DataTrunkApiError,
  DataTrunkResultUnknownError,
  applyPointProcessingPlan,
  createPointProcessingPlan,
  fetchEntityInstanceHistory,
  fetchEntityInstances,
  fetchNodeDataTrunk,
  fetchPointProcessingPlan,
  fetchPointProcessingTemplates,
  previewPointProcessingFormula,
  type EntityHistoryRange,
  type EntityInstance,
  type EntityInstanceObservation,
  type Node,
  type NodeDataTrunk,
  type PointProcessingApplication,
  type PointProcessingFormulaPreview,
  type PointProcessingPlan,
  type PointProcessingTemplate,
} from '../../api/client'
import {
  connectCommittedFrameStream,
  fetchCommittedFrameSnapshot,
} from '../../api/committedFrameStream'
import {
  clearDataTrunkApplyRetry,
  findDataTrunkApplyRetry,
  readDataTrunkApplyRetry,
  saveDataTrunkApplyRetry,
} from './dataTrunkRetryState'
import EntityDataPanel from './EntityDataPanel'
import PointProcessingPlanPanel from './PointProcessingPlanPanel'
import { DATA_TRUNK_STEPS, recommendPointProcessingTemplate } from './dataTrunkViewModel'
import {
  applyFrameDelta,
  replaceSnapshot,
  type CommittedFrameProjection,
} from './committedFrameProjection'

export default function DataTrunkWorkspace({
  node,
  readOnly,
  actorId,
  view,
}: {
  node: Node
  readOnly: boolean
  actorId: string
  view: 'processing' | 'entities'
}) {
  const [trunk, setTrunk] = useState<NodeDataTrunk | null>(null)
  const [templates, setTemplates] = useState<PointProcessingTemplate[]>([])
  const [selectedRevisionId, setSelectedRevisionId] = useState('')
  const [selections, setSelections] = useState<Record<string, string>>({})
  const [plan, setPlan] = useState<PointProcessingPlan | null>(null)
  const [application, setApplication] = useState<PointProcessingApplication | null>(null)
  const [descriptors, setDescriptors] = useState<Map<string, EntityInstance>>(new Map())
  const [projection, setProjection] = useState<CommittedFrameProjection | null>(null)
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null)
  const [entityRange, setEntityRange] = useState<EntityHistoryRange>('1h')
  const [entityHistory, setEntityHistory] = useState<EntityInstanceObservation[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<'plan' | 'apply' | 'formula' | null>(null)
  const [error, setError] = useState('')
  const [resultUnknown, setResultUnknown] = useState(false)
  const [formulaPreview, setFormulaPreview] = useState<PointProcessingFormulaPreview | null>(null)

  const loadRuntime = useCallback(async () => {
    const nextTrunk = await fetchNodeDataTrunk(node.id)
    if (view === 'entities') {
      const catalog = await fetchEntityInstances()
      setDescriptors(new Map(catalog.items.map((item) => [item.id, item])))
    } else {
      setDescriptors(new Map())
    }
    setTrunk(nextTrunk)
    return nextTrunk
  }, [node.id, view])

  const loadWorkspace = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const nextTrunk = await loadRuntime()
      if (view === 'processing' && !readOnly) {
        const nextTemplates = await fetchPointProcessingTemplates((node.node_type || 'PCS').toUpperCase())
        setTemplates(nextTemplates)
        const recommended = recommendPointProcessingTemplate(
          nextTemplates,
          nextTrunk.l0,
          nextTrunk.l1_summary.revision_id,
        )
        setSelectedRevisionId((current) => current || recommended)
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
  }, [actorId, loadRuntime, node.id, node.node_type, readOnly, view])

  useEffect(() => {
    setPlan(null)
    setApplication(null)
    setSelectedRevisionId('')
    setSelections({})
    setResultUnknown(false)
    setFormulaPreview(null)
    setSelectedEntityId(null)
    setEntityRange('1h')
    setEntityHistory([])
    void loadWorkspace()
  }, [loadWorkspace])

  useEffect(() => {
    if (view !== 'entities' || !selectedEntityId) {
      setHistoryLoading(false)
      return
    }
    let active = true
    setHistoryLoading(true)
    setEntityHistory([])
    setError('')
    fetchEntityInstanceHistory(selectedEntityId, entityRange)
      .then((items) => {
        if (active) setEntityHistory(items)
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : '读取实体历史失败')
      })
      .finally(() => {
        if (active) setHistoryLoading(false)
      })
    return () => { active = false }
  }, [entityRange, selectedEntityId, view])

  useEffect(() => {
    let active = true
    let generation = 0
    let stopStream = () => {}
    let controller: AbortController | null = null

    const start = async () => {
      const currentGeneration = ++generation
      stopStream()
      controller?.abort()
      controller = new AbortController()
      setProjection(null)
      try {
        const snapshot = await fetchCommittedFrameSnapshot(node.id, controller.signal)
        if (!active || currentGeneration !== generation) return
        setProjection(replaceSnapshot(null, snapshot))
        stopStream = connectCommittedFrameStream({
          nodeId: node.id,
          cursor: snapshot.cursor,
          onDelta: (delta) => {
            if (!active || currentGeneration !== generation) return
            setProjection((current) => {
              if (!current || current.nodeId !== node.id) return current
              try {
                return applyFrameDelta(current, delta)
              } catch {
                void start()
                return current
              }
            })
          },
          onResnapshotRequired: () => { void start() },
          onError: (reason) => setError(reason.message),
        })
      } catch (reason) {
        if (active && currentGeneration === generation && !(reason instanceof DOMException && reason.name === 'AbortError')) {
          setError(reason instanceof Error ? reason.message : '读取实时帧失败')
        }
      }
    }

    void start()
    return () => {
      active = false
      generation += 1
      controller?.abort()
      stopStream()
    }
  }, [node.id])

  const selectedTemplate = templates.find((item) => item.revision_id === selectedRevisionId) || null
  const recommendedRevisionId = trunk
    ? recommendPointProcessingTemplate(templates, trunk.l0, trunk.l1_summary.revision_id)
    : ''

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
    setFormulaPreview(null)
  }, [selectedRevisionId, trunk?.node_id])

  const handleFormulaPreview = async (expression: string) => {
    if (!selectedTemplate) return
    setBusy('formula')
    setError('')
    try {
      setFormulaPreview(await previewPointProcessingFormula(node.id, {
        template_revision_id: selectedTemplate.revision_id,
        expression,
      }))
    } catch (reason) {
      setFormulaPreview(null)
      setError(reason instanceof Error ? reason.message : '公式预检失败')
    } finally {
      setBusy(null)
    }
  }

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

  const completedStage = application || plan?.status === 'applied'
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
            <h2 className="text-base font-semibold text-gray-900">{view === 'processing' ? '点位加工' : '实体数据'}</h2>
            <p className="mt-1 text-xs text-gray-500">
              {view === 'processing'
                ? '把设备原始点位转换为名称和单位稳定的实体。'
                : '查看供告警、策略、控制和画面使用的实时值与历史值。'}
            </p>
          </div>
          {view === 'processing' && <div className="grid grid-cols-2 gap-1 sm:grid-cols-4">
            {DATA_TRUNK_STEPS.map((step, index) => (
              <div key={step.key} className={`rounded-lg border px-2 py-2 text-center text-[10px] font-medium ${index <= completedStage ? 'border-blue-200 bg-blue-50 text-blue-800' : 'border-gray-200 bg-gray-50 text-gray-500'}`}>
                {step.label}
              </div>
            ))}
          </div>}
        </div>
      </header>

      {error && (
        <div role="alert" className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700">
          {error}
        </div>
      )}

      {view === 'entities' && <EntityDataPanel
        trunk={trunk}
        descriptors={descriptors}
        projection={projection}
        selectedEntityId={selectedEntityId}
        selectedRange={entityRange}
        history={entityHistory}
        historyLoading={historyLoading}
        onSelectEntity={(entityId) => {
          setEntityHistory([])
          setSelectedEntityId((current) => current === entityId ? null : entityId)
        }}
        onRangeChange={setEntityRange}
      />}

      {view === 'processing' && !readOnly && (
        <div className="space-y-3">
          <div className="rounded-xl border border-gray-200 bg-white/45 p-4">
            <h3 className="text-sm font-semibold text-gray-900">当前生效状态</h3>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <div className="border-l-2 border-blue-500 pl-3"><div className="text-[10px] text-gray-500">实体输出</div><div className="mt-1 text-sm font-semibold text-gray-900">{trunk.l1_summary.output_count} 个实体</div></div>
              <div className="border-l-2 border-blue-500 pl-3"><div className="text-[10px] text-gray-500">配置版本</div><div className="mt-1 text-sm font-semibold text-gray-900">{application?.configuration_revision ?? projection?.configurationRevision ?? '未发布'}</div></div>
            </div>
            {application && (
              <div className="mt-3 rounded border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-800">
                发布成功｜配置修订 {application.configuration_revision}｜生成 {trunk.l1_summary.output_count} 个实体
              </div>
            )}
          </div>
          <PointProcessingPlanPanel
            trunk={trunk}
            templates={templates}
            selectedTemplate={selectedTemplate}
            recommendedRevisionId={recommendedRevisionId}
            selections={selections}
            plan={plan}
            busy={busy}
            resultUnknown={resultUnknown}
            formulaPreview={formulaPreview}
            onTemplateChange={setSelectedRevisionId}
            onSelectionChange={(inputId, sourceId) => setSelections((current) => ({ ...current, [inputId]: sourceId }))}
            onPlan={() => void handlePlan()}
            onApply={() => void handleApply()}
            onFormulaPreview={(expression) => void handleFormulaPreview(expression)}
          />
        </div>
      )}
    </div>
  )
}
