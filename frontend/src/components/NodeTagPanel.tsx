import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { deleteRawPoints, fetchTags, maintainRawPoints, type HealthStatus, type Node, type Tag } from '../api/client'
import {
  connectCommittedFrameStream,
  fetchCommittedFrameSnapshot,
} from '../api/committedFrameStream'
import { retryCommittedFrameSnapshot } from '../api/committedFrameRecovery'
import {
  applyFrameDelta,
  replaceSnapshot,
  type CommittedFrameProjection,
} from './data-trunk/committedFrameProjection'
import {
  buildRawPointDataLink,
  projectRawPointValue,
  rawPointReasonLabel,
  RAW_POINT_COLUMNS,
  type RawPointLinkState,
} from './data-trunk/dataTrunkViewModel'
import InlinePointProcessingPanel from './data-trunk/InlinePointProcessingPanel'
import {
  rawPointDisplayNameChange,
  rawPointSelectionSummary,
} from './node/nodeUsabilityModel'

const RawPointHistoryPanel = lazy(() => import('./RawPointHistoryPanel'))

interface NodeTagPanelProps {
  node: Node
  readOnly: boolean
  onPointCountChanged?: () => void
  health: HealthStatus | null
  onRefreshHealth?: () => Promise<void> | void
}

type RawPointView = 'realtime' | 'history'

const PAGE_SIZE = 50

function formatTime(timestamp: string | null | undefined): string {
  return timestamp ? new Date(timestamp).toLocaleString('zh-CN') : '未收到'
}

const LINK_TONES: Record<RawPointLinkState, string> = {
  ok: 'border-green-200 bg-green-50 text-green-800',
  warning: 'border-amber-200 bg-amber-50 text-amber-800',
  error: 'border-red-200 bg-red-50 text-red-800',
  unknown: 'border-gray-200 bg-gray-50 text-gray-500',
}

const LINK_DOTS: Record<RawPointLinkState, string> = {
  ok: 'bg-green-500',
  warning: 'bg-amber-500',
  error: 'bg-red-500',
  unknown: 'bg-gray-400',
}

export default function NodeTagPanel({
  node,
  readOnly,
  onPointCountChanged,
  health,
  onRefreshHealth,
}: NodeTagPanelProps) {
  const nodeId = node.id
  const [view, setView] = useState<RawPointView>('realtime')
  const [tags, setTags] = useState<Tag[]>([])
  const [projection, setProjection] = useState<CommittedFrameProjection | null>(null)
  const [search, setSearch] = useState('')
  const [dataType, setDataType] = useState('')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(1)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<Map<string, Tag>>(new Map())
  const [editingPoint, setEditingPoint] = useState<Tag | null>(null)
  const [displayNameDraft, setDisplayNameDraft] = useState('')
  const [maintenanceBusy, setMaintenanceBusy] = useState(false)
  const [maintenanceMessage, setMaintenanceMessage] = useState('')
  const [realtimeRefresh, setRealtimeRefresh] = useState(0)
  const [refreshing, setRefreshing] = useState(false)
  const activeNodeIdRef = useRef(nodeId)
  const tagRequestGenerationRef = useRef(0)
  activeNodeIdRef.current = nodeId

  useEffect(() => () => {
    tagRequestGenerationRef.current += 1
    activeNodeIdRef.current = ''
  }, [])

  const loadTags = useCallback(async () => {
    const expectedNodeId = nodeId
    const generation = ++tagRequestGenerationRef.current
    setLoading(true)
    setError('')
    try {
      const data = await fetchTags(
        nodeId,
        page,
        PAGE_SIZE,
        search || undefined,
        dataType || undefined,
        'PHYSICAL',
        undefined,
        true,
        'sort_order',
        'asc',
        true,
      )
      if (generation === tagRequestGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) {
        setTags(data.tags)
        setTotal(data.total)
        setTotalPages(data.total_pages || 1)
      }
    } catch {
      if (generation === tagRequestGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) {
        setTags([])
        setError('原始点位读取失败，请稍后重试')
      }
    } finally {
      if (generation === tagRequestGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) setLoading(false)
    }
  }, [dataType, nodeId, page, search])

  useEffect(() => {
    tagRequestGenerationRef.current += 1
    setPage(1)
  }, [nodeId, search, dataType])

  useEffect(() => {
    setSelected(new Map())
  }, [nodeId])

  const refreshRawPoints = async () => {
    if (refreshing) return
    setRefreshing(true)
    await Promise.all([
      loadTags(),
      Promise.resolve(onRefreshHealth?.()),
    ])
    setRealtimeRefresh((value) => value + 1)
  }

  useEffect(() => {
    void loadTags()
  }, [loadTags, node.tag_count])

  useEffect(() => {
    let active = true
    let generation = 0
    let stopStream = () => {}
    let controller: AbortController | null = null

    const start = async () => {
      const currentGeneration = ++generation
      stopStream()
      controller?.abort()
      const requestController = new AbortController()
      controller = requestController
      setProjection(null)
      try {
        const snapshot = await retryCommittedFrameSnapshot(
          () => fetchCommittedFrameSnapshot(nodeId, requestController.signal),
          () => active && currentGeneration === generation,
        )
        if (snapshot === null) return
        if (!active || currentGeneration !== generation) return
        setProjection(replaceSnapshot(null, snapshot))
        stopStream = connectCommittedFrameStream({
          nodeId,
          cursor: snapshot.cursor,
          onDelta: (delta) => {
            if (!active || currentGeneration !== generation) return
            setProjection((current) => {
              if (!current || current.nodeId !== nodeId) return current
              try {
                return applyFrameDelta(current, delta)
              } catch {
                void start()
                return current
              }
            })
          },
          onResnapshotRequired: () => { void start() },
        })
      } catch (reason) {
        if (active && currentGeneration === generation
          && !(reason instanceof DOMException && reason.name === 'AbortError')) {
          setProjection(null)
        }
      } finally {
        if (active && currentGeneration === generation) setRefreshing(false)
      }
    }

    void start()
    return () => {
      active = false
      generation += 1
      controller?.abort()
      stopStream()
    }
  }, [nodeId, realtimeRefresh])

  const selectedRows = [...selected.values()]
  const selectionSummary = rawPointSelectionSummary(selectedRows)
  const selectedPoints = selectedRows.filter((tag) => tag.enabled)
  const selectableTags = tags
  const allVisibleSelected = selectableTags.length > 0
    && selectableTags.every((tag) => selected.has(tag.id))
  const linkTotal = projection?.l0.size || total
  const linkGood = projection
    ? [...projection.l0.values()].filter((item) => item.effective_quality === 192).length
    : 0
  const linkStages = buildRawPointDataLink({
    neuronStatus: health?.components.neuron.status,
    mqttStatus: health?.components.mqtt.status,
    pipelineStatus: health?.pipeline.status,
    lastMessageAt: health?.pipeline.last_message_at,
    frameStatus: projection?.status,
    frameFailureCode: projection?.failure?.code,
    backlogFrames: projection?.backlogFrames || 0,
    projectionAvailable: projection !== null,
    goodPoints: linkGood,
    totalPoints: linkTotal,
  })

  const togglePoint = (tag: Tag) => {
    setSelected((current) => {
      const next = new Map(current)
      if (next.has(tag.id)) next.delete(tag.id)
      else next.set(tag.id, tag)
      return next
    })
  }

  const toggleVisible = () => {
    setSelected((current) => {
      const next = new Map(current)
      if (allVisibleSelected) selectableTags.forEach((tag) => next.delete(tag.id))
      else selectableTags.forEach((tag) => next.set(tag.id, tag))
      return next
    })
  }

  const applyMaintenance = async (
    tagIds: string[],
    changes: { display_name?: string; enabled?: boolean },
    successMessage: string,
  ) => {
    setMaintenanceBusy(true)
    setMaintenanceMessage('')
    try {
      await maintainRawPoints({ tag_ids: tagIds, ...changes })
      setSelected(new Map())
      setEditingPoint(null)
      setMaintenanceMessage(successMessage)
      await loadTags()
    } catch (reason) {
      setMaintenanceMessage(reason instanceof Error ? reason.message : '原始点位维护失败')
    } finally {
      setMaintenanceBusy(false)
    }
  }

  const startEditingDisplayName = () => {
    const point = selectedRows[0]
    if (!point || !selectionSummary.canEditDisplayName) return
    setEditingPoint(point)
    setDisplayNameDraft(point.display_name || point.name)
    setMaintenanceMessage('')
  }

  const saveDisplayName = async () => {
    if (!editingPoint) return
    try {
      const change = rawPointDisplayNameChange(editingPoint.id, displayNameDraft)
      await applyMaintenance(change.tagIds, change.changes, '点位名称已更新')
    } catch (reason) {
      setMaintenanceMessage(reason instanceof Error ? reason.message : '点位名称更新失败')
    }
  }

  const changeSelectedEnabled = async (enabled: boolean) => {
    const targets = selectedRows.filter((tag) => tag.enabled !== enabled)
    if (targets.length === 0) return
    if (!enabled && !window.confirm(`确认停用选中的 ${targets.length} 个原始点位？`)) return
    await applyMaintenance(
      targets.map((tag) => tag.id),
      { enabled },
      enabled ? '原始点位已启用' : '原始点位已停用',
    )
  }

  const deleteSelected = async () => {
    if (!selectionSummary.canDelete) return
    if (!window.confirm(
      `确认永久删除选中的 ${selectedRows.length} 个原始点位？\n点位及全部实时、历史数据将被清除，无法恢复。`,
    )) return
    setMaintenanceBusy(true)
    setMaintenanceMessage('')
    try {
      const result = await deleteRawPoints(selectedRows.map((tag) => tag.id))
      setSelected(new Map())
      setEditingPoint(null)
      setMaintenanceMessage(`已永久删除 ${result.deleted} 个原始点位及其历史数据`)
      await loadTags()
      onPointCountChanged?.()
    } catch (reason) {
      setMaintenanceMessage(reason instanceof Error ? reason.message : '原始点位删除失败')
    } finally {
      setMaintenanceBusy(false)
    }
  }

  return (
    <section className="neu-card min-h-full p-4" aria-label="原始数据">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-bold text-gray-800">原始数据</h3>
          <p className="mt-1 text-xs text-gray-500">查看设备实际上传的值。点位配置请使用节点上方的“导入点位”。</p>
        </div>
        <div className="flex gap-2" aria-label="原始点位数据视图">
          <button
            type="button"
            aria-label="刷新原始点位"
            disabled={refreshing}
            onClick={() => { void refreshRawPoints() }}
            className="neu-btn rounded px-3 py-1.5 text-xs font-medium text-gray-700 disabled:opacity-40"
          >
            {refreshing ? '刷新中...' : '刷新'}
          </button>
          {([
            ['realtime', '实时'],
            ['history', '历史'],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setView(key)}
              className={`rounded px-4 py-1.5 text-xs font-medium ${
                view === key
                  ? 'bg-[#52c41a] text-white'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {view === 'history' ? (
        <Suspense fallback={<div className="py-8 text-center text-xs text-gray-400">历史数据加载中...</div>}>
          <RawPointHistoryPanel nodeId={nodeId} />
        </Suspense>
      ) : (
        <>
          <section className="mb-3" aria-label="数据链路">
            <p className="mb-1.5 text-[10px] text-gray-500">
              前三段是平台公共链路；当前设备是否有数据，以最后的 L0 状态为准。
            </p>
            <div className="flex flex-wrap items-stretch gap-1.5">
              {linkStages.map((stage, index) => (
                <div key={stage.label} className="flex items-center gap-1.5">
                  {index > 0 && <span aria-hidden="true" className="text-gray-300">→</span>}
                  <div className={`min-w-28 rounded border px-2.5 py-2 ${LINK_TONES[stage.state]}`}>
                    <div className="flex items-center gap-1.5 text-[11px] font-semibold">
                      <span className={`h-2 w-2 rounded-full ${LINK_DOTS[stage.state]}`} />
                      {stage.label}
                    </div>
                    <div className="mt-0.5 whitespace-nowrap text-[10px]">{stage.detail}</div>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <div className="mb-3 flex flex-wrap items-center gap-2">
            <label className="text-xs text-gray-600">
              <span className="sr-only">搜索点位</span>
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="搜索点位名称"
                className="neu-inset w-56 rounded px-3 py-2 text-xs text-gray-800 outline-none focus:ring-2 focus:ring-[#52c41a]/30"
              />
            </label>
            <label className="text-xs text-gray-600">
              <span className="sr-only">数据类型</span>
              <select
                value={dataType}
                onChange={(event) => setDataType(event.target.value)}
                className="neu-inset rounded px-3 py-2 text-xs text-gray-700 outline-none focus:ring-2 focus:ring-[#52c41a]/30"
              >
                <option value="">全部类型</option>
                <option value="FLOAT">浮点数</option>
                <option value="INT">整数</option>
                <option value="BOOL">布尔值</option>
                <option value="STRING">字符串</option>
              </select>
            </label>
            <span className="ml-auto text-xs text-gray-500">共 {total} 个点位</span>
          </div>

          {error && (
            <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </div>
          )}

          {!readOnly && (
            <div className="mb-3 rounded border border-gray-200 bg-gray-50 px-3 py-2" aria-label="原始点位维护">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-gray-600">
                  已选 {selectionSummary.count} 个
                </span>
                <button
                  type="button"
                  disabled={!selectionSummary.canEditDisplayName || maintenanceBusy}
                  onClick={startEditingDisplayName}
                  className="neu-btn px-3 py-1.5 text-xs disabled:opacity-40"
                >
                  编辑名称
                </button>
                <button
                  type="button"
                  disabled={!selectionSummary.canEnable || maintenanceBusy}
                  onClick={() => { void changeSelectedEnabled(true) }}
                  className="neu-btn px-3 py-1.5 text-xs disabled:opacity-40"
                >
                  启用
                </button>
                <button
                  type="button"
                  disabled={!selectionSummary.canDisable || maintenanceBusy}
                  onClick={() => { void changeSelectedEnabled(false) }}
                  className="neu-btn px-3 py-1.5 text-xs text-red-700 disabled:opacity-40"
                >
                  停用
                </button>
                <button
                  type="button"
                  disabled={!selectionSummary.canDelete || maintenanceBusy}
                  onClick={() => { void deleteSelected() }}
                  className="neu-btn px-3 py-1.5 text-xs font-medium text-red-700 disabled:opacity-40"
                >
                  删除
                </button>
                <span className="text-[11px] text-gray-500">停用保留数据；删除将永久清除</span>
              </div>

              {editingPoint && (
                <div className="mt-2 flex flex-wrap items-end gap-2 border-t border-gray-200 pt-2" aria-label="编辑原始点位名称">
                  <label className="text-xs text-gray-600">
                    点位显示名称
                    <input
                      value={displayNameDraft}
                      onChange={(event) => setDisplayNameDraft(event.target.value)}
                      className="neu-inset ml-2 w-56 rounded px-3 py-1.5 text-xs text-gray-800 outline-none"
                    />
                  </label>
                  <button type="button" disabled={maintenanceBusy} onClick={() => { void saveDisplayName() }} className="neu-btn px-3 py-1.5 text-xs text-green-700 disabled:opacity-40">保存</button>
                  <button type="button" disabled={maintenanceBusy} onClick={() => setEditingPoint(null)} className="neu-btn px-3 py-1.5 text-xs disabled:opacity-40">取消</button>
                </div>
              )}

              {maintenanceMessage && (
                <p className={`mt-2 text-xs ${maintenanceMessage.includes('已') ? 'text-green-700' : 'text-red-700'}`}>
                  {maintenanceMessage}
                </p>
              )}
            </div>
          )}

          {!readOnly && (
            <InlinePointProcessingPanel
              nodeId={nodeId}
              deviceCategory={node.node_type || 'PCS'}
              points={selectedPoints}
              onPublished={() => undefined}
            />
          )}

          {projection === null && (
            <div className="mb-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              实时数据暂不可用，平台正在自动重试；这不代表设备点位超时。
            </div>
          )}

          <div className="overflow-x-auto rounded border border-gray-200 bg-white">
            <table className="w-full text-left text-xs">
              <thead className="bg-gray-50 text-gray-500">
                <tr>
                  {!readOnly && (
                    <th className="w-10 px-3 py-2 text-center font-medium">
                      <input type="checkbox" aria-label="选择当前页原始点位" checked={allVisibleSelected} onChange={toggleVisible} />
                    </th>
                  )}
                  {RAW_POINT_COLUMNS.map((label) => (
                    <th key={label} className="whitespace-nowrap px-3 py-2 font-medium">{label}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {!loading && tags.map((tag) => {
                  const current = projection?.l0.get(tag.id)
                  const point = projectRawPointValue(
                    current?.value,
                    current?.effective_quality ?? 1,
                    projection !== null,
                  )
                  const qualityClass = point.qualityTone === 'good'
                    ? 'text-green-700'
                    : point.qualityTone === 'uncertain'
                      ? 'text-amber-700'
                      : point.qualityTone === 'stale'
                        ? 'font-semibold text-orange-700'
                        : 'font-semibold text-red-700'
                  return (
                    <tr key={tag.id} className="text-gray-700 hover:bg-gray-50">
                      {!readOnly && (
                        <td className="px-3 py-2 text-center">
                          <input type="checkbox" aria-label={`选择 ${tag.display_name || tag.name}`} checked={selected.has(tag.id)} onChange={() => togglePoint(tag)} />
                        </td>
                      )}
                      <td className="px-3 py-2 font-medium text-gray-800">
                        {tag.display_name || tag.name}
                        {!tag.enabled && <span className="ml-2 rounded bg-gray-200 px-1.5 py-0.5 text-[10px] font-normal text-gray-600">已停用</span>}
                      </td>
                      <td className="px-3 py-2 font-mono text-[11px] text-gray-600">{tag.wire_data_type || tag.data_type}</td>
                      <td className="px-3 py-2 font-mono">{point.displayValue}</td>
                      <td className="px-3 py-2">{current?.unit || tag.unit || '-'}</td>
                      <td className={`px-3 py-2 ${qualityClass}`}>{point.qualityLabel}</td>
                      <td className="max-w-64 px-3 py-2 text-gray-600">
                        {rawPointReasonLabel({
                          reason: current?.reason ?? (current ? null : 'WAITING_DATA'),
                          receivedAt: current?.received_at,
                          frameStatus: projection?.status,
                          frameFailureCode: projection?.failure?.code,
                        })}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2">{formatTime(current?.source_timestamp)}</td>
                      <td className="max-w-80 truncate px-3 py-2 font-mono text-[11px] text-gray-500" title={current?.source_path || tag.source_path || ''}>
                        {current?.source_path || tag.source_path || '未记录'}
                      </td>
                    </tr>
                  )
                })}
                {loading && (
                  <tr><td colSpan={RAW_POINT_COLUMNS.length + (readOnly ? 0 : 1)} className="px-3 py-12 text-center text-gray-400">正在读取原始点位...</td></tr>
                )}
                {!loading && tags.length === 0 && !error && (
                  <tr><td colSpan={RAW_POINT_COLUMNS.length + (readOnly ? 0 : 1)} className="px-3 py-12 text-center text-gray-400">当前节点还没有原始点位</td></tr>
                )}
              </tbody>
            </table>
          </div>

          {totalPages > 1 && (
            <div className="mt-3 flex items-center justify-end gap-2 text-xs text-gray-500">
              <button type="button" disabled={page <= 1} onClick={() => setPage((value) => value - 1)} className="neu-btn px-3 py-1.5 disabled:opacity-40">上一页</button>
              <span>第 {page} / {totalPages} 页</span>
              <button type="button" disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)} className="neu-btn px-3 py-1.5 disabled:opacity-40">下一页</button>
            </div>
          )}
        </>
      )}
    </section>
  )
}
