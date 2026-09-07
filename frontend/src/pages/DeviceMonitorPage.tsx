import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  fetchAlarmCounts,
  fetchEntityInstances,
  fetchNodes,
  type EntityInstance,
  type Node,
} from '../api/client'
import EntityRuntimeDetail from '../components/runtime-monitoring/EntityRuntimeDetail'
import {
  buildDeviceMonitorPage,
  buildRuntimeNodes,
  paginateEntityDetails,
  runtimeEntityReading,
  type RuntimeEntity,
} from '../components/runtime-monitoring/runtimeModel'
import { useRuntimeNodes } from '../components/runtime-monitoring/useRuntimeNodes'
import '../components/runtime-monitoring/runtime-monitoring.css'

export type DeviceMonitorProps = {
  onOpenEngineering?: (nodeId: string) => void
}

function formatValue(value: unknown): string {
  if (Array.isArray(value)) return value.join('、')
  return value == null ? '—' : String(value)
}

function qualityLabel(quality: number | null): string {
  if (quality === 192) return '正常'
  if (quality === 64) return '超时'
  if (quality === 1) return '未知'
  if (quality == null) return '无数据'
  return '异常'
}

function DeviceEntityRow({ entity, nodeCurrent, onOpen }: {
  entity: RuntimeEntity
  nodeCurrent: boolean
  onOpen: () => void
}) {
  const reading = runtimeEntityReading(entity.observation, nodeCurrent)
  const quality = qualityLabel(reading.quality)
  const readingLabel = reading.kind === 'current'
    ? `${quality} · 当前值`
    : reading.kind === 'last'
      ? `${quality} · 最后值（非当前）`
      : `${quality} · 当前状态不确定`
  return (
    <button type="button" onClick={onOpen} className="runtime-device-entity neu-inset">
      <span><strong>{entity.descriptor.display_name}</strong><small>{entity.descriptor.definition_id}</small></span>
      <span className="font-mono-value">{formatValue(reading.value)}{entity.descriptor.unit ? <small> {entity.descriptor.unit}</small> : null}</span>
      <span className={`runtime-quality runtime-quality--${reading.quality ?? 'unknown'}`}>{readingLabel}</span>
    </button>
  )
}

export default function DeviceMonitorPage({ onOpenEngineering }: DeviceMonitorProps) {
  const [nodes, setNodes] = useState<Node[]>([])
  const [descriptors, setDescriptors] = useState<EntityInstance[]>([])
  const [alarmCounts, setAlarmCounts] = useState<Record<string, number> | null>(null)
  const [nodesLoaded, setNodesLoaded] = useState(false)
  const [entitiesLoaded, setEntitiesLoaded] = useState(false)
  const [countsLoaded, setCountsLoaded] = useState(false)
  const [nodesLoading, setNodesLoading] = useState(true)
  const [entitiesLoading, setEntitiesLoading] = useState(true)
  const [countsLoading, setCountsLoading] = useState(true)
  const [nodesError, setNodesError] = useState('')
  const [entitiesError, setEntitiesError] = useState('')
  const [countsError, setCountsError] = useState('')
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('')
  const [onlyAlarms, setOnlyAlarms] = useState(false)
  const [page, setPage] = useState(1)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null)
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null)
  const [entityPage, setEntityPage] = useState(1)
  const [entityPageSize, setEntityPageSize] = useState<10 | 20>(10)
  const nodesGeneration = useRef(0)
  const entitiesGeneration = useRef(0)
  const countsGeneration = useRef(0)

  const loadNodes = useCallback(() => {
    const generation = ++nodesGeneration.current
    setNodesLoading(true)
    setNodesError('')
    void fetchNodes().then((payload) => {
      if (generation !== nodesGeneration.current) return
      setNodes(payload)
      setNodesLoaded(true)
    }).catch((reason) => {
      if (generation !== nodesGeneration.current) return
      setNodesError(reason instanceof Error ? reason.message : '读取设备节点失败。')
    }).finally(() => {
      if (generation === nodesGeneration.current) setNodesLoading(false)
    })
  }, [])

  const loadEntities = useCallback(() => {
    const generation = ++entitiesGeneration.current
    setEntitiesLoading(true)
    setEntitiesError('')
    void fetchEntityInstances().then((payload) => {
      if (generation !== entitiesGeneration.current) return
      setDescriptors(payload.items)
      setEntitiesLoaded(true)
    }).catch((reason) => {
      if (generation !== entitiesGeneration.current) return
      setEntitiesError(reason instanceof Error ? reason.message : '读取 L2 实体目录失败。')
    }).finally(() => {
      if (generation === entitiesGeneration.current) setEntitiesLoading(false)
    })
  }, [])

  const loadCounts = useCallback(() => {
    const generation = ++countsGeneration.current
    setCountsLoading(true)
    setCountsError('')
    void fetchAlarmCounts().then((payload) => {
      if (generation !== countsGeneration.current) return
      setAlarmCounts(payload)
      setCountsLoaded(true)
    }).catch((reason) => {
      if (generation !== countsGeneration.current) return
      setCountsError(reason instanceof Error ? reason.message : '读取告警计数失败。')
      setAlarmCounts(null)
      setOnlyAlarms(false)
    }).finally(() => {
      if (generation === countsGeneration.current) setCountsLoading(false)
    })
  }, [])

  useEffect(() => {
    loadNodes()
    loadEntities()
    loadCounts()
    return () => {
      nodesGeneration.current += 1
      entitiesGeneration.current += 1
      countsGeneration.current += 1
    }
  }, [loadCounts, loadEntities, loadNodes])

  useEffect(() => {
    setPage(1)
    setSelectedNodeId(null)
    setSelectedEntityId(null)
  }, [category, onlyAlarms, query])

  const categories = useMemo(() => [...new Set(nodes.map((node) => node.node_type.trim() || '其他'))].sort((left, right) => left.localeCompare(right, 'zh-CN')), [nodes])
  const monitor = useMemo(() => buildDeviceMonitorPage({
    nodes,
    descriptors,
    alarmCounts,
    query,
    category,
    onlyAlarms,
    page,
  }), [alarmCounts, category, descriptors, nodes, onlyAlarms, page, query])
  const activeNodeIds = useMemo(() => new Set(monitor.activeNodeIds), [monitor.activeNodeIds])
  const activeDescriptors = useMemo(() => descriptors.filter((entity) => activeNodeIds.has(entity.node_id)), [activeNodeIds, descriptors])
  const { states, retryNode } = useRuntimeNodes(activeDescriptors)
  const projections = useMemo(() => new Map(
    [...states.entries()].flatMap(([nodeId, state]) => state.projection ? [[nodeId, state.projection] as const] : []),
  ), [states])
  const runtimeNodes = useMemo(() => buildRuntimeNodes(activeDescriptors, projections), [activeDescriptors, projections])
  const runtimeByNode = useMemo(() => new Map(runtimeNodes.map((node) => [node.nodeId, node])), [runtimeNodes])
  const selectedDevice = monitor.pageItems.find((item) => item.node.id === selectedNodeId) || null
  const selectedRuntime = selectedDevice ? runtimeByNode.get(selectedDevice.node.id) : null
  const detailEntities = selectedRuntime?.entities || selectedDevice?.entities.map((descriptor) => ({ descriptor, observation: null })) || []
  const entityPagination = paginateEntityDetails(detailEntities.map((item) => item.descriptor), entityPage, entityPageSize)
  const visibleDetailEntities = entityPagination.items.map((descriptor) => detailEntities.find((item) => item.descriptor.id === descriptor.id)!).filter(Boolean)
  const selectedEntity = detailEntities.find((entity) => entity.descriptor.id === selectedEntityId) || null

  const goToPage = (nextPage: number) => {
    setPage(nextPage)
    setSelectedNodeId(null)
    setSelectedEntityId(null)
    setEntityPage(1)
  }

  const openDevice = (nodeId: string) => {
    setSelectedNodeId(nodeId)
    setSelectedEntityId(null)
    setEntityPage(1)
  }

  return (
    <section className="runtime-shell runtime-devices">
      <header className="runtime-hero neu-card">
        <div><p className="runtime-eyebrow">自足IOT · 只读运行</p><h2>设备监控</h2><p>按已保存节点类别查看真实 L2；名称只用于显示与搜索，不推测设备身份。</p></div>
        <div className="runtime-hero__actions">
          <button type="button" onClick={() => { loadNodes(); loadEntities(); loadCounts() }} className="neu-btn runtime-touch-button" disabled={nodesLoading || entitiesLoading || countsLoading}>刷新</button>
        </div>
      </header>

      <section className="runtime-device-filters neu-card" aria-label="设备筛选">
        <label><span>名称或 ID</span><input className="neu-input" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索设备名称或 ID" /></label>
        <label><span>节点类别</span><select className="neu-input" value={category} onChange={(event) => setCategory(event.target.value)}><option value="">全部类别</option>{categories.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
        <label className="runtime-device-filters__check"><input type="checkbox" checked={onlyAlarms} disabled={countsLoading || alarmCounts === null || !countsLoaded || Boolean(countsError)} onChange={(event) => setOnlyAlarms(event.target.checked)} /><span>仅有未恢复告警</span></label>
        <div className="runtime-device-filters__summary">{monitor.total} 个节点 · 第 {monitor.page}/{monitor.totalPages} 页</div>
      </section>

      {nodesError && <div role="alert" className="runtime-error neu-card"><span>节点目录读取失败：{nodesError}。现有结果不按空站处理。</span><button type="button" onClick={loadNodes}>重试节点</button></div>}
      {entitiesError && <div role="alert" className="runtime-error neu-card"><span>实体目录读取失败：{entitiesError}。不把未知状态显示为“未配置”。</span><button type="button" onClick={loadEntities}>重试实体</button></div>}
      {countsError && <div role="alert" className="runtime-error neu-card"><span>告警计数读取失败：{countsError}。计数显示未知，且“仅有告警”筛选已关闭。</span><button type="button" onClick={loadCounts}>重试计数</button></div>}

      {nodesLoading && !nodesLoaded ? <div className="runtime-empty neu-card">正在读取真实设备节点…</div> : null}
      {nodesLoaded && nodes.length === 0 ? <div className="runtime-unconfigured neu-card"><strong>当前没有可监控节点</strong><p>设备监控不会生成示例节点或现场拓扑。</p></div> : null}
      {nodesLoaded && nodes.length > 0 && monitor.total === 0 ? <div className="runtime-empty neu-card">当前筛选范围没有匹配节点。</div> : null}

      <div className="runtime-device-grid">
        {monitor.pageItems.map((item) => {
          const runtimeNode = runtimeByNode.get(item.node.id)
          const state = states.get(item.node.id)
          const nodeCurrent = state?.status === 'current' && state.projection?.status === 'COMPLETE'
          return (
            <article key={item.node.id} aria-label={`${item.node.name} 设备卡片`} className="runtime-device-card neu-card">
              <header>
                <div><p>{item.category}</p><h3>{item.node.name}</h3><span>{item.node.id}</span></div>
                <span className={`runtime-device-card__alarm ${item.alarmCount && item.alarmCount > 0 ? 'has-alarm' : ''}`}>未恢复 {countsLoading ? '…' : item.alarmCount ?? '—'}</span>
              </header>
              <div className="runtime-device-card__body">
                {!entitiesLoaded ? <p className="runtime-device-card__empty">{entitiesError ? '实体状态未知' : '正在读取 L2…'}</p> : item.entities.length === 0 ? <p className="runtime-device-card__empty">L2 未配置</p> : (
                  (runtimeNode?.entities || item.entities.map((descriptor) => ({ descriptor, observation: null }))).slice(0, 3).map((entity) => (
                    <DeviceEntityRow key={entity.descriptor.id} entity={entity} nodeCurrent={nodeCurrent} onOpen={() => { openDevice(item.node.id); setSelectedEntityId(entity.descriptor.id) }} />
                  ))
                )}
                {item.entities.length > 3 && <p className="runtime-device-card__more">另有 {item.entities.length - 3} 个实体</p>}
                {state?.error && <p className="runtime-node__notice">实时链路：{state.error} · <button type="button" onClick={() => retryNode(item.node.id)}>重试</button></p>}
              </div>
              <footer>
                <button type="button" className="zizu-primary runtime-touch-button" onClick={() => openDevice(item.node.id)}>查看详情</button>
                {onOpenEngineering && <button type="button" className="neu-btn runtime-touch-button" onClick={() => onOpenEngineering(item.node.id)}>工程配置</button>}
              </footer>
            </article>
          )
        })}
      </div>

      {monitor.total > 0 && <nav className="runtime-pagination neu-card" aria-label="设备分页"><button type="button" className="neu-btn runtime-touch-button" disabled={monitor.page <= 1} onClick={() => goToPage(monitor.page - 1)}>上一页</button><span>第 {monitor.page} / {monitor.totalPages} 页</span><button type="button" className="neu-btn runtime-touch-button" disabled={monitor.page >= monitor.totalPages} onClick={() => goToPage(monitor.page + 1)}>下一页</button></nav>}

      {selectedDevice && (
        <div className="runtime-detail-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) setSelectedNodeId(null) }}>
          <section role="dialog" aria-modal="true" aria-labelledby="device-detail-title" className="runtime-device-detail neu-card">
            <header className="runtime-detail__header"><div><p className="runtime-eyebrow">{selectedDevice.category} · {selectedDevice.node.id}</p><h3 id="device-detail-title">{selectedDevice.node.name}</h3></div><button type="button" className="neu-btn runtime-touch-button" onClick={() => setSelectedNodeId(null)}>关闭</button></header>
            <div className="runtime-device-detail__toolbar"><span>{selectedDevice.entities.length} 个 L2 实体</span><label>每页 <select className="neu-input" value={entityPageSize} onChange={(event) => { setEntityPageSize(Number(event.target.value) === 20 ? 20 : 10); setEntityPage(1) }}><option value={10}>10 条</option><option value={20}>20 条</option></select></label></div>
            {!entitiesLoaded ? <div className="runtime-empty">{entitiesError ? '实体目录不可用，不能判定是否已配置。' : '正在读取 L2 实体目录…'}</div> : selectedDevice.entities.length === 0 ? <div className="runtime-unconfigured"><strong>L2 未配置</strong><p>此节点存在，但尚无可用于运行监控的 L2 全局实体。</p>{onOpenEngineering && <button type="button" className="zizu-primary runtime-touch-button" onClick={() => onOpenEngineering(selectedDevice.node.id)}>配置此节点</button>}</div> : (
              <div className="runtime-device-detail__entities">{visibleDetailEntities.map((entity) => <DeviceEntityRow key={entity.descriptor.id} entity={entity} nodeCurrent={states.get(selectedDevice.node.id)?.status === 'current' && states.get(selectedDevice.node.id)?.projection?.status === 'COMPLETE'} onOpen={() => setSelectedEntityId(entity.descriptor.id)} />)}</div>
            )}
            {selectedDevice.entities.length > entityPageSize && <nav className="runtime-pagination" aria-label="实体分页"><button type="button" className="neu-btn runtime-touch-button" disabled={entityPagination.page <= 1} onClick={() => { setEntityPage(entityPagination.page - 1); setSelectedEntityId(null) }}>上一页</button><span>第 {entityPagination.page} / {entityPagination.totalPages} 页</span><button type="button" className="neu-btn runtime-touch-button" disabled={entityPagination.page >= entityPagination.totalPages} onClick={() => { setEntityPage(entityPagination.page + 1); setSelectedEntityId(null) }}>下一页</button></nav>}
          </section>
        </div>
      )}

      {selectedEntity && (
        <EntityRuntimeDetail descriptor={selectedEntity.descriptor} observation={selectedEntity.observation} nodeCurrent={states.get(selectedEntity.descriptor.node_id)?.status === 'current' && states.get(selectedEntity.descriptor.node_id)?.projection?.status === 'COMPLETE'} onClose={() => setSelectedEntityId(null)} />
      )}
    </section>
  )
}
