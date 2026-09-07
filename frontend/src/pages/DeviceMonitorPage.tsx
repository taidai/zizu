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
  deviceMonitorCategory,
  deviceMonitorDataState,
  deviceMonitorEvidenceTime,
  orderDeviceMonitorEntities,
  paginateEntityDetails,
  runtimeEntityReading,
  summarizeDeviceMonitorDataStates,
  type DeviceMonitorDataState,
  type RuntimeEntity,
} from '../components/runtime-monitoring/runtimeModel'
import { useRuntimeNodes } from '../components/runtime-monitoring/useRuntimeNodes'
import '../components/runtime-monitoring/runtime-monitoring.css'

export type DeviceMonitorProps = {
  onOpenEngineering?: (nodeId: string) => void
}

const CATEGORY_ORDER = ['光伏', '储能', '充电', '电表', '其他'] as const

const DEVICE_STATE_LABEL: Record<DeviceMonitorDataState, string> = {
  current: '数据正常',
  last: '最后值（非当前）',
  unconfigured: '尚未配置',
  unknown: '状态未知',
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

function nodeCurrentStatus(status: string | null | undefined, frameStatus: string | null | undefined): boolean {
  return status === 'current' && frameStatus === 'COMPLETE'
}

function formatEvidenceTime(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

function readingLabel(entity: RuntimeEntity, nodeCurrent: boolean): { label: string; value: string; quality: number | null; evidenceTime: string | null } {
  const reading = runtimeEntityReading(entity.observation, nodeCurrent)
  const quality = qualityLabel(reading.quality)
  return {
    label: reading.kind === 'current'
      ? `${quality} · 当前值`
      : reading.kind === 'last'
        ? `${quality} · 最后值（非当前）`
        : `${quality} · 当前状态不确定`,
    value: formatValue(reading.value),
    quality: reading.quality,
    evidenceTime: deviceMonitorEvidenceTime(reading),
  }
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

  const categories = useMemo(() => {
    const present = new Set(nodes.map((node) => deviceMonitorCategory(node.node_type)))
    return CATEGORY_ORDER.filter((item) => present.has(item))
  }, [nodes])
  const monitor = useMemo(() => buildDeviceMonitorPage({
    nodes,
    descriptors,
    alarmCounts,
    query,
    category,
    onlyAlarms,
    page,
  }), [alarmCounts, category, descriptors, nodes, onlyAlarms, page, query])
  const activeDescriptors = useMemo(() => monitor.pageItems.flatMap((item) => item.entities), [monitor.pageItems])
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
  const dataStates = monitor.pageItems.map((item) => {
    if (!entitiesLoaded) return 'unknown' as const
    const runtimeEntities = runtimeByNode.get(item.node.id)?.entities
      || orderDeviceMonitorEntities(item.node.node_type, item.entities).map((descriptor) => ({ descriptor, observation: null }))
    const state = states.get(item.node.id)
    return deviceMonitorDataState(runtimeEntities, nodeCurrentStatus(state?.status, state?.projection?.status))
  })
  const dataSummary = summarizeDeviceMonitorDataStates(dataStates)
  const abnormalDevices = dataSummary.last + dataSummary.unconfigured + dataSummary.unknown
  const alarmedDevices = alarmCounts === null
    ? null
    : nodes.filter((node) => (alarmCounts[node.id] || 0) > 0).length
  const hasFilters = Boolean(query.trim() || category || onlyAlarms)

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
    setEntityPageSize(10)
  }

  const clearFilters = () => {
    setQuery('')
    setCategory('')
    setOnlyAlarms(false)
    setPage(1)
  }

  return (
    <section className="runtime-shell runtime-devices">
      <header className="runtime-device-heading">
        <div><h2>设备监控</h2><p>设备级 L2 · 只显示真实已提交数据</p></div>
        <dl className="runtime-device-summary" role="region" aria-label="设备监控摘要">
          <div><dt>总设备</dt><dd>{nodesLoaded ? nodes.length : '—'}</dd></div>
          <div><dt>有活动告警</dt><dd className="runtime-device-summary__alarm">{countsLoading ? '…' : alarmedDevices ?? '—'}{alarmedDevices !== null && !countsLoading ? <small>台</small> : null}</dd></div>
          <div><dt>本页数据异常</dt><dd>{entitiesLoading ? '…' : abnormalDevices}<small>台</small></dd></div>
        </dl>
        <button type="button" onClick={() => { loadNodes(); loadEntities(); loadCounts() }} className="neu-btn runtime-touch-button runtime-device-refresh" disabled={nodesLoading || entitiesLoading || countsLoading}>刷新</button>
      </header>

      <section className="runtime-device-filters" aria-label="设备筛选">
        <div className="runtime-device-categories" role="group" aria-label="设备分类">
          <button type="button" className={category === '' ? 'zizu-primary' : 'neu-btn'} aria-pressed={category === ''} onClick={() => setCategory('')}>全部</button>
          {categories.map((item) => <button type="button" key={item} className={category === item ? 'zizu-primary' : 'neu-btn'} aria-pressed={category === item} onClick={() => setCategory(item)}>{item}</button>)}
        </div>
        <label className="runtime-device-search"><span>名称或 ID</span><input aria-label="名称或 ID" className="neu-input" type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索设备名称 / ID" /></label>
        <label className="runtime-device-filters__check"><input type="checkbox" checked={onlyAlarms} disabled={countsLoading || alarmCounts === null || !countsLoaded || Boolean(countsError)} onChange={(event) => setOnlyAlarms(event.target.checked)} /><span>仅有未恢复告警</span></label>
      </section>

      <div className="runtime-device-data-summary" aria-label="本页数据质量">
        <span>当前 {dataSummary.current}</span><span>最后值 {dataSummary.last}</span><span>未配置 {dataSummary.unconfigured}</span><span>未知 {dataSummary.unknown}</span>
      </div>

      {nodesError && <div role="alert" className="runtime-error neu-card"><span>节点目录读取失败：{nodesError}。现有结果不按空站处理。</span><button type="button" onClick={loadNodes}>重试节点</button></div>}
      {entitiesError && <div role="alert" className="runtime-error neu-card"><span>实体目录读取失败：{entitiesError}。不把未知状态显示为“未配置”。</span><button type="button" onClick={loadEntities}>重试实体</button></div>}
      {countsError && <div role="alert" className="runtime-error neu-card"><span>告警计数读取失败：{countsError}。计数显示未知，且“仅有告警”筛选已关闭。</span><button type="button" onClick={loadCounts}>重试计数</button></div>}

      {nodesLoading && !nodesLoaded ? <div className="runtime-empty neu-card">正在读取真实设备节点…</div> : null}
      {nodesLoaded && nodes.length === 0 ? <div className="runtime-unconfigured neu-card"><strong>当前没有可监控节点</strong><p>设备监控不会生成示例节点或现场拓扑。</p></div> : null}
      {nodesLoaded && nodes.length > 0 && monitor.total === 0 ? <div className="runtime-empty neu-card runtime-device-filter-empty"><strong>没有符合条件的设备</strong><p>调整分类、搜索词或告警筛选。</p>{hasFilters && <button type="button" className="neu-btn runtime-touch-button" onClick={clearFilters}>清除筛选</button>}</div> : null}

      <div className="runtime-device-grid">
        {monitor.pageItems.map((item) => {
          const runtimeNode = runtimeByNode.get(item.node.id)
          const state = states.get(item.node.id)
          const nodeCurrent = nodeCurrentStatus(state?.status, state?.projection?.status)
          const cardEntities = runtimeNode?.entities || item.entities.map((descriptor) => ({ descriptor, observation: null }))
          const dataState = entitiesLoaded ? deviceMonitorDataState(cardEntities, nodeCurrent) : 'unknown'
          const primary = cardEntities[0] || null
          const secondary = cardEntities.slice(1, 3)
          const primaryReading = primary ? readingLabel(primary, nodeCurrent) : null
          return (
            <article key={item.node.id} aria-label={`${item.node.name} 设备卡片`} className={`runtime-device-card runtime-device-card--${dataState} neu-card`}>
              <header>
                <div><h3>{item.node.name}</h3><span>{item.category} · {item.node.id}</span></div>
                <span className={`runtime-device-card__state runtime-device-card__state--${dataState}`}><i />{DEVICE_STATE_LABEL[dataState]}</span>
              </header>
              <div className="runtime-device-card__body">
                {!entitiesLoaded ? <p className="runtime-device-card__empty">{entitiesError ? '实体状态未知' : '正在读取 L2…'}</p> : item.entities.length === 0 ? <p className="runtime-device-card__empty">L2 未配置</p> : (
                  <>
                    {primary && primaryReading && <button type="button" className="runtime-device-card__primary" aria-label={primary.descriptor.display_name} onClick={() => { openDevice(item.node.id); setSelectedEntityId(primary.descriptor.id) }}>
                      <span className="runtime-device-card__glyph">{(item.node.node_type || '设备').slice(0, 6).toUpperCase()}</span>
                      <span><small>{primary.descriptor.display_name}</small><strong>{primaryReading.value}<em>{primary.descriptor.unit || ''}</em></strong><span className={`runtime-quality runtime-quality--${primaryReading.quality ?? 'unknown'}`}>{primaryReading.label}</span>{primaryReading.evidenceTime && <time className="runtime-device-card__evidence-time" dateTime={primaryReading.evidenceTime}>最后值 {formatEvidenceTime(primaryReading.evidenceTime)}</time>}</span>
                    </button>}
                    <div className="runtime-device-card__secondary">
                      {secondary.length > 0 ? secondary.map((entity) => {
                        const reading = readingLabel(entity, nodeCurrent)
                        return <button type="button" key={entity.descriptor.id} aria-label={entity.descriptor.display_name} onClick={() => { openDevice(item.node.id); setSelectedEntityId(entity.descriptor.id) }}><span className="runtime-device-card__metric-name">{entity.descriptor.display_name}</span><strong>{reading.value}<small>{entity.descriptor.unit || ''}</small></strong><span className={`runtime-quality runtime-quality--${reading.quality ?? 'unknown'}`}>{reading.label}</span>{reading.evidenceTime && <time className="runtime-device-card__evidence-time" dateTime={reading.evidenceTime}>最后值 {formatEvidenceTime(reading.evidenceTime)}</time>}</button>
                      }) : <span>{item.entities.length} 项全局实体 · 详情中查看</span>}
                    </div>
                  </>
                )}
                {state?.error && <p className="runtime-node__notice">实时链路：{state.error} · <button type="button" onClick={() => retryNode(item.node.id)}>重试</button></p>}
              </div>
              <footer>
                <span className={`runtime-device-card__alarm ${item.alarmCount && item.alarmCount > 0 ? 'has-alarm' : ''}`}>活动告警 <b>{countsLoading ? '…' : item.alarmCount ?? '—'}</b></span>
                <button type="button" className="runtime-device-card__detail runtime-touch-button" onClick={() => openDevice(item.node.id)}>查看详情 <span aria-hidden="true">›</span></button>
              </footer>
            </article>
          )
        })}
      </div>

      {monitor.total > 0 && <nav className="runtime-pagination runtime-device-pagination" aria-label="设备分页"><div><span>共 {monitor.total} 台 · 每页 6 台</span>{hasFilters && <button type="button" className="runtime-device-clear" onClick={clearFilters}>清除筛选</button>}</div><div><button type="button" className="neu-btn runtime-touch-button" disabled={monitor.page <= 1} onClick={() => goToPage(monitor.page - 1)}>上一页</button><span>{monitor.page} / {monitor.totalPages}</span><button type="button" className="neu-btn runtime-touch-button" disabled={monitor.page >= monitor.totalPages} onClick={() => goToPage(monitor.page + 1)}>下一页</button></div></nav>}

      {selectedDevice && (
        <div className="runtime-detail-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) setSelectedNodeId(null) }}>
          <section role="dialog" aria-modal="true" aria-labelledby="device-detail-title" className="runtime-device-detail neu-card">
            <header className="runtime-detail__header"><div><p className="runtime-eyebrow">{selectedDevice.category} · {selectedDevice.node.id}</p><h3 id="device-detail-title">{selectedDevice.node.name}</h3></div><button type="button" className="neu-btn runtime-touch-button" onClick={() => setSelectedNodeId(null)}>关闭</button></header>
            <div className="runtime-device-detail__summary"><span>{selectedDevice.entities.length} 项 L2</span><span className={`runtime-device-card__state runtime-device-card__state--${deviceMonitorDataState(detailEntities, nodeCurrentStatus(states.get(selectedDevice.node.id)?.status, states.get(selectedDevice.node.id)?.projection?.status))}`}><i />{DEVICE_STATE_LABEL[deviceMonitorDataState(detailEntities, nodeCurrentStatus(states.get(selectedDevice.node.id)?.status, states.get(selectedDevice.node.id)?.projection?.status))]}</span><span className="runtime-device-card__alarm">活动告警 <b>{selectedDevice.alarmCount ?? '—'}</b></span></div>
            <div className="runtime-device-detail__toolbar"><span>下表显示实时实体；点击实体打开正式历史趋势与来源证据。</span><label>每页 <select aria-label="实体每页条数" className="neu-input" value={entityPageSize} onChange={(event) => { setEntityPageSize(Number(event.target.value) === 20 ? 20 : 10); setEntityPage(1) }}><option value={10}>10 条</option><option value={20}>20 条</option></select></label></div>
            {!entitiesLoaded ? <div className="runtime-empty">{entitiesError ? '实体目录不可用，不能判定是否已配置。' : '正在读取 L2 实体目录…'}</div> : selectedDevice.entities.length === 0 ? <div className="runtime-unconfigured"><strong>L2 未配置</strong><p>此节点存在，但尚无可用于运行监控的 L2 全局实体。</p>{onOpenEngineering && <button type="button" className="zizu-primary runtime-touch-button" onClick={() => onOpenEngineering(selectedDevice.node.id)}>配置此节点</button>}</div> : (
              <div className="runtime-device-detail__table-wrap"><table className="runtime-device-detail__table"><thead><tr><th>实体名称</th><th>语义键</th><th>类型</th><th>值</th><th>单位</th><th>质量</th></tr></thead><tbody>{visibleDetailEntities.map((entity) => {
                const reading = readingLabel(entity, nodeCurrentStatus(states.get(selectedDevice.node.id)?.status, states.get(selectedDevice.node.id)?.projection?.status))
                return <tr key={entity.descriptor.id}><td><button type="button" aria-label={entity.descriptor.display_name} onClick={() => setSelectedEntityId(entity.descriptor.id)}>{entity.descriptor.display_name}</button></td><td><code>{entity.descriptor.definition_id}</code></td><td>{entity.descriptor.data_type}</td><td className="font-mono-value">{reading.value}</td><td>{entity.descriptor.unit || '—'}</td><td><span className={`runtime-quality runtime-quality--${reading.quality ?? 'unknown'}`}>{reading.label}</span></td></tr>
              })}</tbody></table></div>
            )}
            {selectedDevice.entities.length > entityPageSize && <nav className="runtime-pagination" aria-label="实体分页"><button type="button" className="neu-btn runtime-touch-button" disabled={entityPagination.page <= 1} onClick={() => { setEntityPage(entityPagination.page - 1); setSelectedEntityId(null) }}>上一页</button><span>第 {entityPagination.page} / {entityPagination.totalPages} 页</span><button type="button" className="neu-btn runtime-touch-button" disabled={entityPagination.page >= entityPagination.totalPages} onClick={() => { setEntityPage(entityPagination.page + 1); setSelectedEntityId(null) }}>下一页</button></nav>}
            {onOpenEngineering && <footer className="runtime-device-detail__footer"><span>运行监控只消费 L2，不直接读取 L0。</span><button type="button" className="neu-btn runtime-touch-button" onClick={() => onOpenEngineering(selectedDevice.node.id)}>配置此设备</button></footer>}
          </section>
        </div>
      )}

      {selectedEntity && (
        <EntityRuntimeDetail descriptor={selectedEntity.descriptor} observation={selectedEntity.observation} l0={[...(states.get(selectedEntity.descriptor.node_id)?.projection?.l0.values() || [])]} nodeCurrent={nodeCurrentStatus(states.get(selectedEntity.descriptor.node_id)?.status, states.get(selectedEntity.descriptor.node_id)?.projection?.status)} onClose={() => setSelectedEntityId(null)} />
      )}
    </section>
  )
}
