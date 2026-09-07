export interface NodeCandidate {
  id: string
  parent_id: string | null
  layer: number
  name: string
}

export interface PointCatalogState {
  nodeId: string
  page: number
  pageSize: 10 | 20
  search: string
  dataType: string
  selectedIds: string[]
}

export function changePointCatalogScope(
  current: PointCatalogState,
  change: Partial<Pick<PointCatalogState, 'nodeId' | 'pageSize' | 'search' | 'dataType'>>,
): PointCatalogState {
  return {
    ...current,
    ...change,
    page: 1,
    selectedIds: [],
  }
}

export function initialNodeSelection(
  nodes: readonly { id: string; parent_id: string | null }[],
  currentId: string,
  requestedId?: string,
): string {
  if (currentId && nodes.some((node) => node.id === currentId)) return currentId
  if (requestedId && nodes.some((node) => node.id === requestedId)) return requestedId
  return nodes.find((node) => !node.parent_id)?.id || nodes[0]?.id || ''
}

export function parentCandidates<T extends NodeCandidate>(nodes: T[], editedNodeId?: string): T[] {
  if (!editedNodeId) return nodes.filter((node) => node.layer < 5)
  const excluded = new Set([editedNodeId])
  let changed = true
  while (changed) {
    changed = false
    for (const node of nodes) {
      if (node.parent_id && excluded.has(node.parent_id) && !excluded.has(node.id)) {
        excluded.add(node.id)
        changed = true
      }
    }
  }
  return nodes.filter((node) => !excluded.has(node.id) && node.layer < 5)
}

export function normalizedGroups(groups: string[]): string[] {
  return Array.from(new Set(groups.map((group) => group.trim()).filter(Boolean))).sort()
}

export interface NeuronSelectionSnapshot {
  generation: number
  neuronNode: string
  groups: string[]
}

export function captureNeuronSelection(
  generation: number,
  neuronNode: string,
  groups: string[],
): NeuronSelectionSnapshot {
  return {
    generation,
    neuronNode,
    groups: normalizedGroups(groups),
  }
}

export function neuronPreviewBelongsToSelection(
  preview: { neuron_node: string; selected_groups: string[] },
  requested: NeuronSelectionSnapshot,
  current: NeuronSelectionSnapshot,
): boolean {
  const sameGroups = (left: string[], right: string[]) => (
    left.length === right.length && left.every((group, index) => group === right[index])
  )
  return requested.generation === current.generation
    && requested.neuronNode === current.neuronNode
    && sameGroups(requested.groups, current.groups)
    && preview.neuron_node === requested.neuronNode
    && sameGroups(normalizedGroups(preview.selected_groups), requested.groups)
}

export function neuronImportReconciliation(preview: {
  counts?: Partial<Record<'create' | 'update' | 'unchanged' | 'conflict', number>>
  has_conflicts?: boolean
}): 'applied' | 'fresh-preview' {
  const summary = importPreviewSummary(preview)
  return summary.create === 0 && summary.update === 0 && summary.conflict === 0 && !preview.has_conflicts
    ? 'applied'
    : 'fresh-preview'
}

export function importPreviewSummary(preview: {
  counts?: Partial<Record<'create' | 'update' | 'unchanged' | 'conflict', number>>
  has_conflicts?: boolean
}) {
  const create = preview.counts?.create ?? 0
  const update = preview.counts?.update ?? 0
  const unchanged = preview.counts?.unchanged ?? 0
  const conflict = preview.counts?.conflict ?? 0
  return {
    create,
    update,
    unchanged,
    conflict,
    canApply: !preview.has_conflicts && conflict === 0,
    label: `新增 ${create} · 更新 ${update} · 不变 ${unchanged} · 冲突 ${conflict}`,
  }
}

const IMPORT_ACTION_LABELS = {
  create: '新增',
  update: '更新',
  unchanged: '不变',
  conflict: '冲突',
} as const

export function importPreviewRows(preview: {
  items: Array<{
    source_path: string
    group: string
    name: string
    source_address: string
    action: keyof typeof IMPORT_ACTION_LABELS
    reason?: string | null
  }>
}) {
  return preview.items.map((item) => ({
    key: item.source_path,
    source: `${item.group} · ${item.name} · ${item.source_address}`,
    action: item.action,
    actionLabel: IMPORT_ACTION_LABELS[item.action],
    reason: item.reason || '—',
  }))
}

export function rawPointSelectionSummary(points: { id: string; enabled: boolean }[]) {
  return {
    count: points.length,
    canEditDisplayName: points.length === 1,
    canEnable: points.some((point) => !point.enabled),
    canDisable: points.some((point) => point.enabled),
    canDelete: points.length > 0,
  }
}

export function rawPointDisplayNameChange(tagId: string, displayName: string) {
  const normalized = displayName.trim()
  if (!normalized) throw new Error('请输入点位显示名称')
  return {
    tagIds: [tagId],
    changes: { display_name: normalized },
  }
}
