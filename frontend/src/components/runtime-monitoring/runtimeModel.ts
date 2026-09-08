import type { EntityInstance, EntityInstanceObservation, Node, NodeDataTrunk } from '../../api/client'
import type { CommittedFrameSnapshot, L0FrameItem, L2FrameItem } from '../../api/committedFrameStream'
import type { CommittedFrameProjection } from '../data-trunk/committedFrameProjection'

type RuntimeFrame = CommittedFrameSnapshot | CommittedFrameProjection

type ProvenanceSource = {
  kind: 'l0' | 'l2'
  inputId: string
  sourceKey: string
  point?: L0FrameItem
  error?: string
}

export function entityProvenanceModel({ descriptor, observation, trunk, l0 }: {
  descriptor: EntityInstance
  observation: L2FrameItem | null
  trunk: NodeDataTrunk | null
  l0: readonly L0FrameItem[]
}): { error: string | null; processingKind?: string; revisionId?: string; sources: ProvenanceSource[] } {
  const unavailable = (error: string) => ({ error, sources: [] })
  if (!trunk) return unavailable('节点数据主干尚未读取。')
  if (trunk.node_id !== descriptor.node_id) return unavailable('节点数据主干身份不匹配。')
  if (!observation || observation.entity_instance_id !== descriptor.id || observation.node_id !== descriptor.node_id
    || observation.definition_id !== descriptor.definition_id) return unavailable('所选 L2 提交观测缺失或身份不匹配。')
  const revisionId = trunk.l1_summary?.revision_id
  if (!trunk.l1_summary?.installed || !revisionId || revisionId !== observation.processing_revision_id) {
    return unavailable('当前 L1 加工修订与 L2 提交观测不匹配。')
  }
  const installedRevision = trunk.l1_summary.configuration_revision
  if (!Number.isInteger(installedRevision) || installedRevision == null || installedRevision <= 0
    || installedRevision !== observation.configuration_revision) {
    return unavailable('所选观测来源证据不可用：L1 安装配置修订缺失或与 L2 观测配置修订不匹配；当前主干仅可作为当前配置参考。')
  }
  const outputs = trunk.l2?.filter((item) => item.entity_instance_id === descriptor.id) || []
  if (outputs.length !== 1) return unavailable('所选 L2 的 L1 输出映射缺失或存在歧义。')
  const output = outputs[0]
  if (!output.processing_kind) return unavailable('L1 加工类型缺失。')
  if (!output.source_summary?.length) return unavailable('L1 输出来源映射缺失。')
  const sources = output.source_summary.map((source): ProvenanceSource => {
    const base = { kind: source.source_kind, inputId: source.input_id, sourceKey: source.source_key }
    if (!source.source_key || !['l0', 'l2'].includes(source.source_kind)) return { ...base, error: '来源种类或稳定键缺失。' }
    if (source.source_kind === 'l2') return base
    const catalog = trunk.l0?.filter((item) => item.source_key === source.source_key) || []
    // The current API declares tag.name as the L0 stable key. Never use display names or path suffixes.
    const points = l0.filter((point) => point.node_id === descriptor.node_id && (
      catalog.length ? catalog.some((item) => item.source_id === point.tag_id) : point.name === source.source_key
    ))
    if (catalog.length > 1 || points.length > 1) return { ...base, error: `L0 来源 ${source.source_key} 存在歧义。` }
    if (!points.length) return { ...base, error: `已提交投影缺少 L0 来源 ${source.source_key}。` }
    const point = points[0]
    if (!Number.isFinite(point.frame_sequence) || point.frame_sequence > observation.frame_sequence) {
      return { ...base, error: `L0 来源 ${source.source_key} 的帧证据晚于或无法对应 L2。` }
    }
    return { ...base, point }
  })
  return { error: null, processingKind: output.processing_kind, revisionId, sources }
}

export interface RuntimeEntity {
  descriptor: EntityInstance
  observation: L2FrameItem | null
}

export interface RuntimeNode {
  nodeId: string
  nodeName: string
  nodeType: string
  entities: RuntimeEntity[]
}

export type RuntimeReading = {
  kind: 'current' | 'last' | 'unknown'
  value: L2FrameItem['value']
  quality: number | null
  observedAt: string | null
  valueObservedAt: string | null
}

function frameNodeId(frame: RuntimeFrame): string {
  return 'nodeId' in frame ? frame.nodeId : frame.node_id
}

function frameEntities(frame: RuntimeFrame): L2FrameItem[] {
  return frame.l2 instanceof Map ? [...frame.l2.values()] : frame.l2
}

function matchingObservation(
  descriptor: EntityInstance,
  frame: RuntimeFrame | undefined,
): L2FrameItem | null {
  if (!frame || frameNodeId(frame) !== descriptor.node_id) return null
  const observation = frameEntities(frame).find((item) => (
    item.entity_instance_id === descriptor.id
    && item.node_id === descriptor.node_id
    && item.definition_id === descriptor.definition_id
  ))
  return observation ? { ...observation } : null
}

export function buildRuntimeNodes(
  descriptors: EntityInstance[],
  frames: ReadonlyMap<string, RuntimeFrame>,
): RuntimeNode[] {
  const nodes = new Map<string, RuntimeNode>()
  for (const descriptor of descriptors) {
    const existing = nodes.get(descriptor.node_id)
    const node = existing || {
      nodeId: descriptor.node_id,
      nodeName: descriptor.node_display_name,
      nodeType: descriptor.node_type,
      entities: [],
    }
    node.entities.push({
      descriptor,
      observation: matchingObservation(descriptor, frames.get(descriptor.node_id)),
    })
    if (!existing) nodes.set(descriptor.node_id, node)
  }
  return [...nodes.values()]
}

export function runtimeEntityReading(
  observation: L2FrameItem | null,
  nodeCurrent: boolean,
): RuntimeReading {
  if (!observation || observation.value == null) {
    return {
      kind: 'unknown',
      value: null,
      quality: observation?.quality ?? null,
      observedAt: observation?.observed_at ?? null,
      valueObservedAt: observation?.value_observed_at ?? null,
    }
  }
  return {
    kind: nodeCurrent && observation.quality === 192 ? 'current' : 'last',
    value: observation.value,
    quality: observation.quality,
    observedAt: observation.observed_at,
    valueObservedAt: observation.value_observed_at,
  }
}

export function deviceMonitorEvidenceTime(reading: RuntimeReading): string | null {
  if (reading.kind !== 'last') return null
  return reading.valueObservedAt || reading.observedAt
}

export function numericHistorySegments(
  points: Array<{ observed_at: string; value: unknown; quality: number }>,
): Array<Array<{ time: number; value: number }>> {
  const result: Array<Array<{ time: number; value: number }>> = []
  let segment: Array<{ time: number; value: number }> = []
  for (const point of points) {
    const time = Date.parse(point.observed_at)
    if (point.quality !== 192 || typeof point.value !== 'number' || !Number.isFinite(point.value) || !Number.isFinite(time)) {
      if (segment.length) result.push(segment)
      segment = []
    } else segment.push({ time, value: point.value })
  }
  if (segment.length) result.push(segment)
  return result
}

export function entityHistoryModel(
  descriptor: EntityInstance,
  points: EntityInstanceObservation[],
): {
  unit: string | null
  points: EntityInstanceObservation[]
  numericSegments: Array<Array<{ time: number; value: number }>>
} {
  const scoped = points.filter((point) => (
    point.entity_instance_id === descriptor.id
    && point.definition_id === descriptor.definition_id
    && point.unit === descriptor.unit
  ))
  return {
    unit: descriptor.unit,
    points: scoped,
    numericSegments: numericHistorySegments(scoped),
  }
}

export interface DeviceMonitorItem {
  node: Node
  category: DeviceMonitorCategory
  entities: EntityInstance[]
  alarmCount: number | null
}

export type DeviceMonitorCategory = '光伏' | '储能' | '充电' | '电表' | '其他'
export type DeviceMonitorDataState = 'current' | 'last' | 'unconfigured' | 'unknown'

const DEVICE_CATEGORY_BY_NODE_TYPE: Readonly<Record<string, DeviceMonitorCategory>> = {
  PV: '光伏',
  INVERTER: '光伏',
  PV_INVERTER: '光伏',
  SOLAR: '光伏',
  ESS: '储能',
  STORAGE: '储能',
  PCS: '储能',
  BMS: '储能',
  BATTERY: '储能',
  EVSE: '充电',
  CHARGER: '充电',
  CHARGING: '充电',
  METER: '电表',
  GRID: '电表',
  LOAD: '电表',
}

const DEVICE_METRIC_PRIORITY: Readonly<Record<string, readonly string[]>> = {
  PV: ['pv.activePower', 'pv.active_power', 'pv.dailyEnergy', 'pv.daily_energy', 'pv.inverterTemp', 'pv.inverter_temp', 'pv.status'],
  INVERTER: ['pv.activePower', 'pv.active_power', 'pv.dailyEnergy', 'pv.daily_energy', 'pv.inverterTemp', 'pv.inverter_temp', 'pv.status'],
  PV_INVERTER: ['pv.activePower', 'pv.active_power', 'pv.dailyEnergy', 'pv.daily_energy', 'pv.inverterTemp', 'pv.inverter_temp', 'pv.status'],
  SOLAR: ['pv.activePower', 'pv.active_power', 'pv.dailyEnergy', 'pv.daily_energy', 'pv.inverterTemp', 'pv.inverter_temp', 'pv.status'],
  ESS: ['ess.activePower', 'ess.active_power', 'ess.soc', 'ess.soh', 'ess.status'],
  STORAGE: ['ess.activePower', 'ess.active_power', 'ess.soc', 'ess.soh', 'ess.status'],
  PCS: ['pcs.activePower', 'pcs.active_power', 'pcs.dischargePowerLimit', 'pcs.discharge_power_limit', 'pcs.temp', 'pcs.temperature', 'pcs.status', 'pcs.running_state'],
  BMS: ['ess.soc', 'bms.soc', 'ess.soh', 'bms.soh', 'ess.voltage', 'ess.current', 'ess.status'],
  BATTERY: ['ess.soc', 'bms.soc', 'ess.soh', 'bms.soh', 'ess.voltage', 'ess.current', 'ess.status'],
  EVSE: ['charger.chargingPower', 'charger.active_power', 'charger.soc', 'charger.chargedEnergy', 'charger.status'],
  CHARGER: ['charger.chargingPower', 'charger.active_power', 'charger.soc', 'charger.chargedEnergy', 'charger.status'],
  CHARGING: ['charger.chargingPower', 'charger.active_power', 'charger.soc', 'charger.chargedEnergy', 'charger.status'],
  METER: ['grid.activePower', 'grid.active_power', 'ems.loadPowerTotal', 'ems.load_power_total', 'grid.frequency', 'grid.powerFactor'],
  GRID: ['grid.activePower', 'grid.active_power', 'grid.frequency', 'grid.powerFactor'],
  LOAD: ['ems.loadPowerTotal', 'ems.load_power_total', 'grid.activePower', 'grid.active_power', 'grid.frequency'],
}

function normalizedNodeType(nodeType: string | null | undefined): string {
  return (nodeType || '').trim().toUpperCase()
}

export function deviceMonitorCategory(nodeType: string | null | undefined): DeviceMonitorCategory {
  return DEVICE_CATEGORY_BY_NODE_TYPE[normalizedNodeType(nodeType)] || '其他'
}

export function orderDeviceMonitorEntities(
  nodeType: string | null | undefined,
  entities: readonly EntityInstance[],
): EntityInstance[] {
  const priorities = DEVICE_METRIC_PRIORITY[normalizedNodeType(nodeType)] || []
  const ranks = new Map(priorities.map((definitionId, index) => [definitionId, index]))
  return [...entities].sort((left, right) => {
    const leftRank = ranks.get(left.definition_id) ?? Number.MAX_SAFE_INTEGER
    const rightRank = ranks.get(right.definition_id) ?? Number.MAX_SAFE_INTEGER
    return leftRank - rightRank
      || left.definition_id.localeCompare(right.definition_id, 'en')
      || left.id.localeCompare(right.id, 'en')
  })
}

export function deviceMonitorDataState(
  entities: readonly RuntimeEntity[] | null,
  nodeCurrent: boolean,
): DeviceMonitorDataState {
  if (entities === null) return 'unknown'
  if (entities.length === 0) return 'unconfigured'
  const readings = entities.map((entity) => runtimeEntityReading(entity.observation, nodeCurrent))
  if (readings.some((reading) => reading.kind === 'unknown')) return 'unknown'
  return readings.every((reading) => reading.kind === 'current') ? 'current' : 'last'
}

export function summarizeDeviceMonitorDataStates(
  states: readonly DeviceMonitorDataState[],
): Record<DeviceMonitorDataState, number> {
  const result: Record<DeviceMonitorDataState, number> = { current: 0, last: 0, unconfigured: 0, unknown: 0 }
  for (const state of states) result[state] += 1
  return result
}

export function buildDeviceMonitorPage({
  nodes,
  descriptors,
  alarmCounts,
  query,
  category,
  onlyAlarms,
  page,
}: {
  nodes: Node[]
  descriptors: EntityInstance[]
  alarmCounts: Readonly<Record<string, number>> | null
  query: string
  category: string
  onlyAlarms: boolean
  page: number
}): {
  page: number
  total: number
  totalPages: number
  pageItems: DeviceMonitorItem[]
  activeNodeIds: string[]
  alarmFilterBlocked: boolean
} {
  const normalizedQuery = query.trim().toLocaleLowerCase()
  const alarmFilterBlocked = onlyAlarms && alarmCounts === null
  const entitiesByNode = new Map<string, EntityInstance[]>()
  for (const descriptor of descriptors) {
    const current = entitiesByNode.get(descriptor.node_id) || []
    current.push(descriptor)
    entitiesByNode.set(descriptor.node_id, current)
  }
  const filteredNodes = alarmFilterBlocked ? [] : nodes
    .filter((node) => {
      const nodeCategory = deviceMonitorCategory(node.node_type)
      if (category && nodeCategory !== category) return false
      if (normalizedQuery && !node.name.toLocaleLowerCase().includes(normalizedQuery) && !node.id.toLocaleLowerCase().includes(normalizedQuery)) return false
      if (onlyAlarms && (alarmCounts?.[node.id] ?? 0) <= 0) return false
      return true
    })
    .sort((left, right) => left.sort_order - right.sort_order || left.name.localeCompare(right.name, 'zh-CN') || left.id.localeCompare(right.id))
  const totalPages = Math.max(1, Math.ceil(filteredNodes.length / 6))
  const safePage = Math.min(Math.max(1, page), totalPages)
  const pageItems = filteredNodes.slice((safePage - 1) * 6, safePage * 6).map((node) => ({
    node,
    category: deviceMonitorCategory(node.node_type),
    entities: orderDeviceMonitorEntities(node.node_type, entitiesByNode.get(node.id) || []),
    alarmCount: alarmCounts === null ? null : alarmCounts[node.id] ?? 0,
  }))
  const activeNodeIds = pageItems.map((item) => item.node.id)
  return {
    page: safePage,
    total: filteredNodes.length,
    totalPages,
    pageItems,
    activeNodeIds,
    alarmFilterBlocked,
  }
}

export function paginateEntityDetails(
  entities: EntityInstance[],
  page: number,
  requestedPageSize: 10 | 20,
): { page: number; pageSize: 10 | 20; totalPages: number; items: EntityInstance[] } {
  const pageSize = requestedPageSize === 20 ? 20 : 10
  const totalPages = Math.max(1, Math.ceil(entities.length / pageSize))
  const safePage = Math.min(Math.max(1, page), totalPages)
  return {
    page: safePage,
    pageSize,
    totalPages,
    items: entities.slice((safePage - 1) * pageSize, safePage * pageSize),
  }
}
