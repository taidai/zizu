import { useCallback, useEffect, useRef, useState } from 'react'
import {
  DataTrunkApiError,
  DataTrunkResultUnknownError,
  applyPointProcessingPlan,
  createPointProcessingDeactivationPlan,
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
import PointProcessingTemplateManager from './PointProcessingTemplateManager'
import {
  initialPointProcessingSelections,
  isCurrentNodeResult,
  pointProcessingDeactivationSummary,
  recommendPointProcessingTemplate,
  selectedInputBindings,
} from './dataTrunkViewModel'
import {
  applyFrameDelta,
  replaceSnapshot,
  type CommittedFrameProjection,
} from './committedFrameProjection'
import { requestResultIsCurrent } from '../rawPointHistoryModel'

type BusyAction = 'plan' | 'apply' | 'formula' | 'edit-apply' | 'deactivate-plan' | 'deactivate-apply' | null

function isDeactivationPlan(plan: PointProcessingPlan): boolean {
  return plan.items.some((item) => item.action === 'delete_candidate')
}

export default function DataTrunkWorkspace({
  node,
  readOnly,
  actorId,
  canManageTemplates,
}: {
  node: Node
  readOnly: boolean
  actorId: string
  canManageTemplates: boolean
}) {
  const [trunk, setTrunk] = useState<NodeDataTrunk | null>(null)
  const [templates, setTemplates] = useState<PointProcessingTemplate[]>([])
  const [selectedRevisionId, setSelectedRevisionId] = useState('')
  const [selections, setSelections] = useState<Record<string, string>>({})
  const [plan, setPlan] = useState<PointProcessingPlan | null>(null)
  const [editPlan, setEditPlan] = useState<PointProcessingPlan | null>(null)
  const [deactivationPlan, setDeactivationPlan] = useState<PointProcessingPlan | null>(null)
  const [application, setApplication] = useState<PointProcessingApplication | null>(null)
  const [descriptors, setDescriptors] = useState<Map<string, EntityInstance>>(new Map())
  const [projection, setProjection] = useState<CommittedFrameProjection | null>(null)
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null)
  const [entityRange, setEntityRange] = useState<EntityHistoryRange>('1h')
  const [entityHistory, setEntityHistory] = useState<EntityInstanceObservation[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<BusyAction>(null)
  const [error, setError] = useState('')
  const [resultUnknownPlanId, setResultUnknownPlanId] = useState<string | null>(null)
  const [formulaPreview, setFormulaPreview] = useState<PointProcessingFormulaPreview | null>(null)
  const [lifecycleOpen, setLifecycleOpen] = useState(!readOnly)
  const [lifecycleSection, setLifecycleSection] = useState<'node' | 'library'>('node')
  const activeNodeIdRef = useRef(node.id)
  const workspaceGenerationRef = useRef(0)
  const operationGenerationRef = useRef(0)
  activeNodeIdRef.current = node.id

  useEffect(() => () => {
    workspaceGenerationRef.current += 1
    operationGenerationRef.current += 1
    activeNodeIdRef.current = ''
  }, [])

  const loadRuntime = useCallback(async (expectedNodeId = node.id): Promise<NodeDataTrunk | null> => {
    const [nextTrunk, catalog] = await Promise.all([
      fetchNodeDataTrunk(expectedNodeId),
      fetchEntityInstances(),
    ])
    if (!isCurrentNodeResult(nextTrunk.node_id, activeNodeIdRef.current)
      || activeNodeIdRef.current !== expectedNodeId) return null
    setDescriptors(new Map(catalog.items.map((item) => [item.id, item])))
    setTrunk(nextTrunk)
    return nextTrunk
  }, [node.id])

  const loadWorkspace = useCallback(async () => {
    const expectedNodeId = node.id
    const generation = ++workspaceGenerationRef.current
    setLoading(true)
    setError('')
    try {
      const nextTrunk = await loadRuntime(expectedNodeId)
      if (!nextTrunk || !requestResultIsCurrent({
        requestGeneration: generation,
        currentGeneration: workspaceGenerationRef.current,
        expectedNodeId,
        currentNodeId: activeNodeIdRef.current,
        resultNodeId: nextTrunk.node_id,
      })) return

      if (!readOnly) {
        const nextTemplates = await fetchPointProcessingTemplates((node.node_type || 'DEVICE').toUpperCase())
        if (generation !== workspaceGenerationRef.current
          || activeNodeIdRef.current !== expectedNodeId) return
        setTemplates(nextTemplates)
        setSelectedRevisionId(recommendPointProcessingTemplate(
          nextTemplates,
          nextTrunk.l0,
          nextTrunk.l1_summary.revision_id,
        ))

        const retry = findDataTrunkApplyRetry(sessionStorage, actorId, expectedNodeId)
        if (retry) {
          try {
            const restoredPlan = await fetchPointProcessingPlan(retry.planId)
            if (!isCurrentNodeResult(restoredPlan.node_id, activeNodeIdRef.current)) return
            if (readDataTrunkApplyRetry(sessionStorage, {
              actorId,
              nodeId: expectedNodeId,
              planId: restoredPlan.id,
              planDigest: restoredPlan.digest,
            })) {
              if (isDeactivationPlan(restoredPlan)) setDeactivationPlan(restoredPlan)
              else if (nextTemplates.some((template) => template.revision_id === restoredPlan.template_revision_id)) {
                setPlan(restoredPlan)
                setSelectedRevisionId(restoredPlan.template_revision_id)
              } else setEditPlan(restoredPlan)
              setResultUnknownPlanId(restoredPlan.id)
            }
          } catch {
            clearDataTrunkApplyRetry(sessionStorage)
          }
        }
      }
    } catch (reason) {
      if (generation === workspaceGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) {
        setError(reason instanceof Error ? reason.message : '读取标准实体失败')
      }
    } finally {
      if (generation === workspaceGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) setLoading(false)
    }
  }, [actorId, loadRuntime, node.id, node.node_type, readOnly])

  useEffect(() => {
    workspaceGenerationRef.current += 1
    operationGenerationRef.current += 1
    setTrunk(null)
    setTemplates([])
    setDescriptors(new Map())
    setPlan(null)
    setEditPlan(null)
    setDeactivationPlan(null)
    setApplication(null)
    setSelectedRevisionId('')
    setSelections({})
    setResultUnknownPlanId(null)
    setFormulaPreview(null)
    setSelectedEntityId(null)
    setEntityRange('1h')
    setEntityHistory([])
    setLifecycleSection('node')
    void loadWorkspace()
  }, [loadWorkspace])

  useEffect(() => {
    if (!selectedEntityId) {
      setHistoryLoading(false)
      return
    }
    let active = true
    setHistoryLoading(true)
    setEntityHistory([])
    setError('')
    fetchEntityInstanceHistory(selectedEntityId, entityRange)
      .then((items) => { if (active) setEntityHistory(items) })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : '读取实体历史失败')
      })
      .finally(() => { if (active) setHistoryLoading(false) })
    return () => { active = false }
  }, [entityRange, selectedEntityId])

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
              try { return applyFrameDelta(current, delta) }
              catch {
                void start()
                return current
              }
            })
          },
          onResnapshotRequired: () => { void start() },
          onError: (reason) => setError(reason.message),
        })
      } catch (reason) {
        if (active && currentGeneration === generation
          && !(reason instanceof DOMException && reason.name === 'AbortError')) {
          setError(reason instanceof Error ? reason.message : '读取实体实时数据失败')
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
  const installedTemplate = templates.find((item) => item.revision_id === trunk?.l1_summary.revision_id) || null
  const recommendedRevisionId = trunk
    ? recommendPointProcessingTemplate(templates, trunk.l0, trunk.l1_summary.revision_id)
    : ''

  useEffect(() => {
    if (!selectedTemplate || !trunk) return
    setSelections(initialPointProcessingSelections(selectedTemplate, trunk))
    setFormulaPreview(null)
  }, [selectedRevisionId, trunk?.node_id])

  const handleFormulaPreview = async (expression: string) => {
    if (!selectedTemplate) return
    const expectedNodeId = node.id
    const generation = ++operationGenerationRef.current
    setBusy('formula')
    setError('')
    try {
      const preview = await previewPointProcessingFormula(expectedNodeId, {
        template_revision_id: selectedTemplate.revision_id,
        expression,
      })
      if (generation === operationGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) setFormulaPreview(preview)
    } catch (reason) {
      if (generation === operationGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) {
        setFormulaPreview(null)
        setError(reason instanceof Error ? reason.message : '公式预检失败')
      }
    } finally {
      if (generation === operationGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) setBusy(null)
    }
  }

  const handlePlan = async () => {
    if (!selectedTemplate) return
    const expectedNodeId = node.id
    const generation = ++operationGenerationRef.current
    setBusy('plan')
    setError('')
    clearDataTrunkApplyRetry(sessionStorage)
    setResultUnknownPlanId(null)
    setEditPlan(null)
    setDeactivationPlan(null)
    try {
      const nextPlan = await createPointProcessingPlan(expectedNodeId, {
        template_revision_id: selectedTemplate.revision_id,
        input_selections: selectedInputBindings(selections),
      })
      if (!requestResultIsCurrent({
        requestGeneration: generation,
        currentGeneration: operationGenerationRef.current,
        expectedNodeId,
        currentNodeId: activeNodeIdRef.current,
        resultNodeId: nextPlan.node_id,
      })) return
      setPlan(nextPlan)
    } catch (reason) {
      if (generation === operationGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) {
        setError(reason instanceof Error ? reason.message : '生成计划失败')
      }
    } finally {
      if (generation === operationGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) setBusy(null)
    }
  }

  const applyPreparedPlan = async (preparedPlan: PointProcessingPlan, mode: 'apply' | 'edit-apply' | 'deactivate-apply') => {
    if (!isCurrentNodeResult(preparedPlan.node_id, node.id)) {
      setPlan(null)
      setEditPlan(null)
      setDeactivationPlan(null)
      setError('节点已经切换，请重新检查当前节点的加工结果。')
      return
    }
    const expectedNodeId = node.id
    const generation = ++operationGenerationRef.current
    setBusy(mode)
    setError('')
    const identity = {
      actorId,
      nodeId: expectedNodeId,
      planId: preparedPlan.id,
      planDigest: preparedPlan.digest,
    }
    const existing = readDataTrunkApplyRetry(sessionStorage, identity)
    const retry = existing || { ...identity, idempotencyKey: crypto.randomUUID() }
    saveDataTrunkApplyRetry(sessionStorage, retry)
    try {
      const result = await applyPointProcessingPlan(
        preparedPlan.id,
        preparedPlan.digest,
        retry.idempotencyKey,
      )
      if (generation !== operationGenerationRef.current
        || activeNodeIdRef.current !== expectedNodeId) {
        clearDataTrunkApplyRetry(sessionStorage)
        return
      }
      setApplication(result)
      if (mode === 'deactivate-apply') setDeactivationPlan({ ...preparedPlan, status: 'applied' })
      else if (mode === 'edit-apply') setEditPlan({ ...preparedPlan, status: 'applied' })
      else setPlan({ ...preparedPlan, status: 'applied' })
      clearDataTrunkApplyRetry(sessionStorage)
      setResultUnknownPlanId(null)
      await loadRuntime(expectedNodeId)
    } catch (reason) {
      const shouldKeep = reason instanceof DataTrunkResultUnknownError
        || reason instanceof TypeError
        || (reason instanceof DataTrunkApiError && reason.retryable)
      if (!shouldKeep) clearDataTrunkApplyRetry(sessionStorage)
      if (generation === operationGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) {
        setResultUnknownPlanId(shouldKeep ? preparedPlan.id : null)
        setError(reason instanceof Error ? reason.message : '应用点位加工失败')
      }
    } finally {
      if (generation === operationGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) setBusy(null)
    }
  }

  const handlePrepareDeactivation = async () => {
    const expectedNodeId = node.id
    const generation = ++operationGenerationRef.current
    setBusy('deactivate-plan')
    setError('')
    clearDataTrunkApplyRetry(sessionStorage)
    setResultUnknownPlanId(null)
    try {
      const nextPlan = await createPointProcessingDeactivationPlan(expectedNodeId)
      if (!requestResultIsCurrent({
        requestGeneration: generation,
        currentGeneration: operationGenerationRef.current,
        expectedNodeId,
        currentNodeId: activeNodeIdRef.current,
        resultNodeId: nextPlan.node_id,
      })) return
      setPlan(null)
      setEditPlan(null)
      setDeactivationPlan(nextPlan)
    } catch (reason) {
      if (generation === operationGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) {
        setError(reason instanceof Error ? reason.message : '准备停用点位加工失败')
      }
    } finally {
      if (generation === operationGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) setBusy(null)
    }
  }

  const refreshTemplates = async (revisionId: string, openNodeConfiguration: boolean) => {
    const nextTemplates = await fetchPointProcessingTemplates((node.node_type || 'DEVICE').toUpperCase())
    if (activeNodeIdRef.current !== node.id) return
    setTemplates(nextTemplates)
    setSelectedRevisionId(revisionId)
    setPlan(null)
    setEditPlan(null)
    setResultUnknownPlanId(null)
    if (openNodeConfiguration) setLifecycleSection('node')
  }

  if (loading) {
    return <div className="neu-card p-6 text-sm text-gray-500">正在读取标准实体...</div>
  }
  if (!trunk) {
    return (
      <div className="neu-card p-6">
        <p className="text-sm font-semibold text-gray-800">标准实体不可用</p>
        <p className="mt-1 text-xs text-red-600">{error || '请检查节点和平台连接。'}</p>
        <button type="button" onClick={() => void loadWorkspace()} className="neu-btn mt-4 px-3 py-2 text-xs text-blue-700">重新读取</button>
      </div>
    )
  }

  const deactivationSummary = deactivationPlan
    ? pointProcessingDeactivationSummary(deactivationPlan)
    : null
  const processingBusy = busy === 'plan' || busy === 'apply' || busy === 'formula' ? busy : null

  return (
    <div className="space-y-3 pb-3">
      <header className="rounded-xl border border-gray-200 bg-white/55 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-gray-900">标准实体</h2>
            <p className="mt-1 text-xs text-gray-500">先说明数据从哪里来、怎样计算，再把稳定实体交给告警、JDM、控制和 EMS 工作台。</p>
          </div>
          <div className="text-[10px] text-gray-500">
            配置修订 {application?.configuration_revision ?? projection?.configurationRevision ?? '等待数据'}
          </div>
        </div>
      </header>

      {error && <div role="alert" className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700">{error}</div>}

      <section className="rounded-xl border border-gray-200 bg-white/55" aria-label="数据来源与计算">
        <button
          type="button"
          onClick={() => setLifecycleOpen((current) => !current)}
          className="flex w-full flex-wrap items-center justify-between gap-3 px-4 py-3 text-left"
        >
          <span>
            <span className="block text-sm font-semibold text-gray-900">数据来源与计算</span>
            <span className="mt-1 block text-xs text-gray-500">
              {trunk.l1_summary.installed
                ? `${trunk.l1_summary.output_count} 个实体正在由点位加工生成`
                : '尚未配置点位加工；可直接从原始数据创建，或安装一个模板。'}
            </span>
          </span>
          <span className="text-xs font-medium text-blue-700">{lifecycleOpen ? '收起' : readOnly ? '查看来源' : '管理'}</span>
        </button>

        {lifecycleOpen && (
          <div className="space-y-4 border-t border-gray-200 p-4">
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-lg border border-gray-200 bg-white p-3">
                <div className="text-[10px] text-gray-500">当前状态</div>
                <div className={`mt-1 text-sm font-semibold ${trunk.l1_summary.installed ? 'text-green-700' : 'text-gray-700'}`}>
                  {trunk.l1_summary.installed ? '已生效' : '未配置'}
                </div>
              </div>
              <div className="rounded-lg border border-gray-200 bg-white p-3">
                <div className="text-[10px] text-gray-500">加工版本</div>
                <div className="mt-1 truncate text-sm font-semibold text-gray-800" title={trunk.l1_summary.revision_id || ''}>
                  {installedTemplate ? `${installedTemplate.display_name} · 修订 ${installedTemplate.revision}` : trunk.l1_summary.revision_id ? '本节点自定义加工' : '—'}
                </div>
              </div>
              <div className="rounded-lg border border-gray-200 bg-white p-3">
                <div className="text-[10px] text-gray-500">输出</div>
                <div className="mt-1 text-sm font-semibold text-gray-800">{trunk.l1_summary.output_count} 个标准实体</div>
              </div>
            </div>

            {trunk.l1_summary.source_summary.length > 0 && (
              <div className="rounded-lg bg-gray-50 px-3 py-2 text-[11px] text-gray-600">
                当前来源：{trunk.l1_summary.source_summary.map((item) => item.source_key).join('、')}
              </div>
            )}

            {!readOnly && (
              <>
                <div className="flex flex-wrap gap-2 border-b border-gray-200 pb-3">
                  <button type="button" onClick={() => setLifecycleSection('node')} className={`rounded-lg px-4 py-2 text-xs font-medium ${lifecycleSection === 'node' ? 'bg-[#52c41a] text-white' : 'bg-white text-gray-600'}`}>
                    本节点配置
                  </button>
                  <button type="button" onClick={() => setLifecycleSection('library')} className={`rounded-lg px-4 py-2 text-xs font-medium ${lifecycleSection === 'library' ? 'bg-[#185FA5] text-white' : 'bg-white text-gray-600'}`}>
                    模板与版本
                  </button>
                </div>

                {lifecycleSection === 'library' && (
                  <PointProcessingTemplateManager
                    templates={templates}
                    selectedRevisionId={selectedRevisionId}
                    l0Points={trunk.l0}
                    nodeName={node.name}
                    deviceCategory={(node.node_type || 'DEVICE').toUpperCase()}
                    nodeId={node.id}
                    currentRevisionId={trunk.l1_summary.revision_id}
                    currentInputBindings={trunk.l1_summary.input_bindings || {}}
                    currentPlan={editPlan}
                    currentApplyBusy={busy === 'edit-apply'}
                    currentResultUnknown={resultUnknownPlanId === editPlan?.id}
                    canConfigure={!readOnly}
                    canManage={canManageTemplates}
                    onCurrentPlan={setEditPlan}
                    onApplyCurrentPlan={() => { if (editPlan) void applyPreparedPlan(editPlan, 'edit-apply') }}
                    onPublished={(revisionId) => refreshTemplates(revisionId, true)}
                  />
                )}

                {lifecycleSection === 'node' && (
                  <div className="space-y-4">
                    {templates.length > 0 ? (
                      <PointProcessingPlanPanel
                        trunk={trunk}
                        templates={templates}
                        selectedTemplate={selectedTemplate}
                        recommendedRevisionId={recommendedRevisionId}
                        selections={selections}
                        plan={plan}
                        busy={processingBusy}
                        resultUnknown={resultUnknownPlanId === plan?.id}
                        formulaPreview={formulaPreview}
                        onTemplateChange={(revisionId) => {
                          setSelectedRevisionId(revisionId)
                          setPlan(null)
                          setResultUnknownPlanId(null)
                        }}
                        onSelectionChange={(inputId, sourceId) => setSelections((current) => ({ ...current, [inputId]: sourceId }))}
                        onPlan={() => void handlePlan()}
                        onApply={() => { if (plan) void applyPreparedPlan(plan, 'apply') }}
                        onFormulaPreview={(expression) => void handleFormulaPreview(expression)}
                      />
                    ) : (
                      <div className="rounded-lg border border-dashed border-gray-300 px-4 py-6 text-center text-xs text-gray-600">
                        还没有适配此类设备的模板。管理员可到“模板与版本”从当前原始点位创建第一个模板。
                      </div>
                    )}

                    {trunk.l1_summary.installed && !deactivationPlan && (
                      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-red-100 bg-red-50/50 p-3">
                        <div>
                          <div className="text-xs font-semibold text-gray-800">不再使用这套加工？</div>
                          <div className="mt-1 text-[11px] text-gray-600">先生成停用预览；不会删除历史数据和来源证据。</div>
                        </div>
                        <button type="button" disabled={busy !== null} onClick={() => void handlePrepareDeactivation()} className="neu-btn px-3 py-2 text-xs font-medium text-red-700 disabled:opacity-50">
                          {busy === 'deactivate-plan' ? '正在检查…' : '准备停用'}
                        </button>
                      </div>
                    )}

                    {deactivationPlan && (
                      <div className={`rounded-lg border p-4 ${deactivationPlan.status === 'applied' ? 'border-green-200 bg-green-50' : deactivationSummary?.canApply ? 'border-amber-200 bg-amber-50' : 'border-red-200 bg-red-50'}`}>
                        <div className="text-sm font-semibold text-gray-900">
                          {deactivationPlan.status === 'applied' ? '已停用点位加工' : '停用预览'}
                        </div>
                        <p className="mt-1 text-xs text-gray-700">{deactivationSummary?.message}</p>
                        {deactivationPlan.blockers.length > 0 && (
                          <p role="alert" className="mt-2 text-xs text-red-700">仍有上层计算依赖这些实体，暂时不能停用。</p>
                        )}
                        {resultUnknownPlanId === deactivationPlan.id && (
                          <p role="alert" className="mt-2 text-xs text-amber-800">结果暂时未知，请使用同一按钮重试，系统不会重复执行。</p>
                        )}
                        <div className="mt-3 flex flex-wrap justify-end gap-2">
                          {deactivationPlan.status !== 'applied' && (
                            <button type="button" disabled={busy !== null} onClick={() => setDeactivationPlan(null)} className="neu-btn px-3 py-2 text-xs text-gray-600 disabled:opacity-50">取消</button>
                          )}
                          {deactivationPlan.status === 'ready' && deactivationSummary?.canApply && (
                            <button type="button" disabled={busy !== null} onClick={() => void applyPreparedPlan(deactivationPlan, 'deactivate-apply')} className="rounded-lg bg-red-700 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">
                              {busy === 'deactivate-apply' ? '正在停用…' : '确认停用'}
                            </button>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </section>

      <EntityDataPanel
        nodeId={node.id}
        canManageTemplates={canManageTemplates}
        trunk={trunk}
        descriptors={descriptors}
        projection={projection}
        selectedEntityId={selectedEntityId}
        selectedRange={entityRange}
        history={entityHistory}
        historyLoading={historyLoading}
        onTemplatePromoted={(revisionId) => {
          void refreshTemplates(revisionId, false).catch((reason) => {
            setError(reason instanceof Error ? reason.message : '刷新模板列表失败')
          })
        }}
        onSelectEntity={(entityId) => {
          setEntityHistory([])
          setSelectedEntityId((current) => current === entityId ? null : entityId)
        }}
        onRangeChange={setEntityRange}
      />
    </div>
  )
}
