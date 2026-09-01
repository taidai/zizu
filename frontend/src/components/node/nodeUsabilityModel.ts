export interface NodeCandidate {
  id: string
  parent_id: string | null
  layer: number
  name: string
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
