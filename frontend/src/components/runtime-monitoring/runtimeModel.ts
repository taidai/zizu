import type { EntityInstance, EntityInstanceObservation, Node } from '../../api/client'
import type { CommittedFrameSnapshot, L2FrameItem } from '../../api/committedFrameStream'
import type { CommittedFrameProjection } from '../data-trunk/committedFrameProjection'

type RuntimeFrame = CommittedFrameSnapshot | CommittedFrameProjection

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
  category: string
  entities: EntityInstance[]
  alarmCount: number | null
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
      const nodeCategory = node.node_type.trim() || '其他'
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
    category: node.node_type.trim() || '其他',
    entities: entitiesByNode.get(node.id) || [],
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
