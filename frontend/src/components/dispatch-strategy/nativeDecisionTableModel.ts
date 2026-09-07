type GraphNode = { id: string; type: string; content?: unknown; [key: string]: unknown }
export type NativeDecisionGraph = { nodes: GraphNode[]; [key: string]: unknown }

type EntityCandidate = {
  id?: string
  confirmed?: boolean
  direction?: string
  data_type?: string
  unit?: string | null
  freshness_seconds?: number
  control_eligible?: boolean
}

type StrategyBinding = {
  direction: string
  binding_key: string
  ordinal: number
  entity_instance_id: string
  expected_data_type: string
  unit: string | null
  freshness_seconds: number
}

const NATIVE_TYPES = new Set(['BOOL', 'BOOLEAN', 'INT', 'FLOAT', 'NUMBER', 'NUMERIC', 'DOUBLE', 'DECIMAL', 'STRING', 'STATE', 'ENUM'])

export function inspectNativeDecisionTable(graph: NativeDecisionGraph | null | undefined): { nodeId: string; content: unknown } | null {
  if (!Array.isArray(graph?.nodes)) return null
  const candidates = graph.nodes.filter((node) => node.type === 'decisionTableNode')
  if (candidates.length !== 1 || !candidates[0].content || typeof candidates[0].content !== 'object') return null
  const content = candidates[0].content as { inputs?: unknown; outputs?: unknown; rules?: unknown; hitPolicy?: unknown }
  if (!Array.isArray(content.inputs) || !Array.isArray(content.outputs) || !Array.isArray(content.rules)) return null
  if (content.hitPolicy !== 'first' && content.hitPolicy !== 'collect') return null
  return { nodeId: candidates[0].id, content: candidates[0].content }
}

export function replaceDecisionTableContent(graph: NativeDecisionGraph, nodeId: string, content: unknown): NativeDecisionGraph {
  const candidates = graph.nodes.filter((node) => node.type === 'decisionTableNode')
  if (candidates.length !== 1 || candidates[0].id !== nodeId) throw new Error('请使用完整规则图编辑此策略')
  return {
    ...graph,
    nodes: graph.nodes.map((node) => node.id === nodeId ? { ...node, content } : node),
  }
}

export function isNativeDecisionInputEntity(entity: EntityCandidate | null | undefined): boolean {
  return !!entity?.confirmed
    && ['R', 'RW'].includes(entity.direction || '')
    && NATIVE_TYPES.has(String(entity.data_type).toUpperCase())
}

export function isNativeDecisionOutputEntity(entity: EntityCandidate | null | undefined): boolean {
  return !!entity?.confirmed
    && entity.control_eligible === true
    && ['W', 'RW'].includes(entity.direction || '')
    && NATIVE_TYPES.has(String(entity.data_type).toUpperCase())
}

export function validateBindingAliases(bindings: Pick<StrategyBinding, 'direction' | 'binding_key'>[]): { valid: boolean; message: string } {
  const seen = new Set<string>()
  for (const binding of bindings) {
    const alias = binding.binding_key.trim()
    if (!alias) return { valid: false, message: '输入和输出别名不能为空。' }
    const identity = `${binding.direction}:${alias}`
    if (seen.has(identity)) return { valid: false, message: '同一方向的每个实体别名必须唯一。' }
    seen.add(identity)
  }
  return { valid: true, message: '' }
}

export function updateStrategyBinding<T extends StrategyBinding>(
  bindings: T[],
  index: number,
  entity: EntityCandidate,
  alias: string,
): T[] {
  if (index < 0 || index >= bindings.length) throw new Error('找不到要编辑的实体绑定。')
  const current = bindings[index]
  return bindings.map((binding, itemIndex) => itemIndex === index ? {
    ...current,
    binding_key: alias.trim(),
    entity_instance_id: entity.id,
    expected_data_type: String(entity.data_type).toUpperCase(),
    unit: entity.unit ?? null,
    freshness_seconds: Number(entity.freshness_seconds),
  } as T : binding)
}
