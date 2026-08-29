import { useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchEntityInstanceHistory,
  fetchEntityInstances,
  fetchNodeDataTrunk,
  type EntityHistoryRange,
  type EntityInstance,
  type EntityInstanceObservation,
  type Node,
  type NodeDataTrunk,
} from '../../api/client'
import {
  connectCommittedFrameStream,
  fetchCommittedFrameSnapshot,
} from '../../api/committedFrameStream'
import {
  applyFrameDelta,
  replaceSnapshot,
  type CommittedFrameProjection,
} from './committedFrameProjection'
import EntityDataPanel from './EntityDataPanel'

export default function DataTrunkWorkspace({
  node,
  canManageTemplates,
}: {
  node: Node
  canManageTemplates: boolean
}) {
  const [trunk, setTrunk] = useState<NodeDataTrunk | null>(null)
  const [descriptors, setDescriptors] = useState<Map<string, EntityInstance>>(new Map())
  const [projection, setProjection] = useState<CommittedFrameProjection | null>(null)
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null)
  const [entityRange, setEntityRange] = useState<EntityHistoryRange>('1h')
  const [entityHistory, setEntityHistory] = useState<EntityInstanceObservation[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const activeNodeIdRef = useRef(node.id)
  const requestGenerationRef = useRef(0)
  activeNodeIdRef.current = node.id

  const loadWorkspace = useCallback(async () => {
    const expectedNodeId = node.id
    const generation = ++requestGenerationRef.current
    setLoading(true)
    setError('')
    try {
      const [nextTrunk, catalog] = await Promise.all([
        fetchNodeDataTrunk(expectedNodeId),
        fetchEntityInstances(),
      ])
      if (
        generation !== requestGenerationRef.current
        || activeNodeIdRef.current !== expectedNodeId
        || nextTrunk.node_id !== expectedNodeId
      ) return
      setTrunk(nextTrunk)
      setDescriptors(new Map(catalog.items.map((item) => [item.id, item])))
    } catch (reason) {
      if (generation === requestGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) {
        setError(reason instanceof Error ? reason.message : '读取标准实体失败')
      }
    } finally {
      if (generation === requestGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) setLoading(false)
    }
  }, [node.id])

  useEffect(() => {
    requestGenerationRef.current += 1
    setTrunk(null)
    setDescriptors(new Map())
    setSelectedEntityId(null)
    setEntityRange('1h')
    setEntityHistory([])
    void loadWorkspace()
  }, [loadWorkspace])

  useEffect(() => () => {
    requestGenerationRef.current += 1
    activeNodeIdRef.current = ''
  }, [])

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

  return (
    <div className="space-y-3 pb-3">
      <header className="rounded-xl border border-gray-200 bg-white/55 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-base font-semibold text-gray-900">标准实体</h2>
            <p className="mt-1 text-xs text-gray-500">查看供告警、JDM、控制和 EMS 工作台使用的实时值与历史值。</p>
          </div>
          <div className="text-[10px] text-gray-500">
            配置修订 {projection?.configurationRevision ?? '等待数据'}
          </div>
        </div>
      </header>
      {error && <div role="alert" className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700">{error}</div>}
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
        onSelectEntity={(entityId) => {
          setEntityHistory([])
          setSelectedEntityId((current) => current === entityId ? null : entityId)
        }}
        onRangeChange={setEntityRange}
      />
    </div>
  )
}
