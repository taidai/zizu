/**
 * ZiZu API Client
 * 封装后端 REST + WebSocket 调用
 */

import {
  clearAuthSession,
  getAuthSession,
  invalidateAuthSession,
  setAuthSession,
  updateAuthUser,
  type AuthSession,
  type AuthUser,
} from './authSession'

const API_BASE = '/api/v1'
const WS_URL = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api/v1/ws/telemetry`

/**
 * Single HTTP seam for the frontend. Authentication is resolved at request
 * time so a restored or replaced session is used without rebuilding clients.
 */
async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const session = getAuthSession()
  const requestToken = session?.accessToken
  const headers = new Headers(init.headers)
  if (requestToken) headers.set('Authorization', `Bearer ${requestToken}`)

  const response = await fetch(input, { ...init, headers })
  if (response.status === 401 && requestToken) invalidateAuthSession(requestToken)
  return response
}

async function authError(response: Response, fallback: string): Promise<Error> {
  const payload = await response.json().catch(() => null) as {
    detail?: string | { message?: string }
  } | null
  const detail = payload?.detail
  const message = typeof detail === 'string' ? detail : detail?.message
  return new Error(message || fallback)
}

export async function login(username: string, password: string): Promise<AuthSession> {
  // Login is the only HTTP call that intentionally bypasses apiFetch.
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!response.ok) throw await authError(response, `Login failed: ${response.status}`)
  const payload = await response.json() as {
    access_token: string
    expires_at: string
    user: AuthUser
  }
  const session = {
    accessToken: payload.access_token,
    expiresAt: payload.expires_at,
    user: payload.user,
  }
  setAuthSession(session)
  return session
}

export async function fetchCurrentUser(): Promise<AuthUser> {
  const response = await apiFetch(`${API_BASE}/auth/me`)
  if (!response.ok) throw await authError(response, `Session validation failed: ${response.status}`)
  const payload = await response.json() as { user: AuthUser }
  updateAuthUser(payload.user)
  return payload.user
}

export async function logout(): Promise<void> {
  const token = getAuthSession()?.accessToken
  if (!token) return
  try {
    const response = await apiFetch(`${API_BASE}/auth/logout`, { method: 'POST' })
    if (!response.ok && response.status !== 401) {
      throw await authError(response, `Logout failed: ${response.status}`)
    }
  } finally {
    clearAuthSession(token)
  }
}

async function downloadAuthenticated(url: string, fallbackName: string): Promise<void> {
  const response = await apiFetch(url)
  if (!response.ok) throw new Error(`Download failed: ${response.status}`)
  const blobUrl = URL.createObjectURL(await response.blob())
  const disposition = response.headers.get('Content-Disposition') || ''
  const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
  const plainName = disposition.match(/filename="?([^";]+)"?/i)?.[1]
  const filename = encodedName ? decodeURIComponent(encodedName) : plainName || fallbackName
  try {
    const link = document.createElement('a')
    link.href = blobUrl
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
  } finally {
    URL.revokeObjectURL(blobUrl)
  }
}

// ── Types ──

export interface Node {
  id: string
  name: string
  parent_id: string | null
  layer: number
  node_type: string
  config?: Record<string, any>
  sort_order: number
  enabled: boolean
  tag_count: number
}

export interface Tag {
  id: string
  node_id: string
  node_name: string
  name: string
  display_name: string | null
  data_type: string
  tag_type: string
  unit: string | null
  scale_factor: number
  value_offset: number
  source_path: string | null
  read_write: string
  enabled: boolean
  description: string | null
  raw_value: number | null
  eng_value: number | null
  latest_ts: string | null
  quality: number | null
  aggregate_fn: string | null
  formula: string | null
  formula_type: string | null
  sources: string[] | null
  alarm_level: string | null
  alarm_type: string | null
  alarm_threshold: number | null
  fault_map_id: string | null
  fault_map_name: string | null
}

export interface HealthStatus {
  status: string
  version: string
  uptime_seconds: number
  components: {
    timescaledb: { status: string }
    mqtt: { status: string }
    neuron: { status: string }
  }
  pipeline: {
    status: string
    messages_received: number
    points_written_db: number
    last_message_at: string | null
  }
  validation?: {
    mqtt_connection: { status: string; message: string }
    message_parsing: { status: string; success_rate: number; parse_errors: number }
    normalization: { status: string; points_normalized: number; unmatched_rules: number }
    db_write: { status: string; write_errors: number; buffered_records: number; last_write_at: string | null }
  }
}

export interface NeuronNode {
  name: string
  plugin: string
  state?: number
}

export interface NeuronGroup {
  name: string
  interval: number
}

export interface NeuronTag {
  name: string
  address: string
  type?: number
}

export interface Category {
  id: string
  name: string
  node_type: string
  description: string | null
}

export interface TelemetryUpdate {
  tag_id: string
  raw_value: number | null
  eng_value: number | null
  ts: string | null
  quality: number
}

// ── REST API ──

export async function fetchNodes(): Promise<Node[]> {
  const res = await apiFetch(`${API_BASE}/nodes`)
  const data = await res.json()
  return data.nodes || []
}

// ── Node Tree API (F3) ──

export interface TreeNode {
  id: string
  name: string
  parent_id: string | null
  layer: number
  node_type: string
  config: Record<string, any>
  sort_order: number
  enabled: boolean
  tag_count: number
  children: TreeNode[]
}

export interface NodeTag {
  id: string
  name: string
  display_name: string | null
  data_type: string
  tag_type: string
  unit: string | null
  scale_factor: number
  value_offset: number
  source_path: string | null
  read_write: string
  enabled: boolean
}

/** 以 rootId 为根拉取整棵子树 (最大 5 层)。 */
export async function fetchNodeTree(rootId: string): Promise<TreeNode | null> {
  const res = await apiFetch(`${API_BASE}/nodes/${rootId}/tree`)
  if (!res.ok) throw new Error(`Fetch tree failed: ${res.status}`)
  const data = await res.json()
  return data.tree || null
}

/** 获取单个节点详情 (含其 tags 列表，用于实时值订阅)。 */
export async function fetchNodeDetail(nodeId: string): Promise<{ node: Node; tags: NodeTag[] }> {
  const res = await apiFetch(`${API_BASE}/nodes/${nodeId}`)
  if (!res.ok) throw new Error(`Fetch node failed: ${res.status}`)
  return res.json()
}

export interface NodeCreateInput {
  name: string
  parent_id?: string | null
  layer: number
  node_type?: string | null
  config?: Record<string, any>
  sort_order?: number
  enabled?: boolean
}

export async function createNode(input: NodeCreateInput): Promise<{ node: Node }> {
  const res = await apiFetch(`${API_BASE}/nodes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `Create node failed: ${res.status}`)
  }
  return res.json()
}

export async function deleteNode(nodeId: string): Promise<{ deleted: string; cascade_nodes: number }> {
  const res = await apiFetch(`${API_BASE}/nodes/${nodeId}`, { method: 'DELETE' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `Delete node failed: ${res.status}`)
  }
  return res.json()
}

export interface TagCreateInput {
  node_id: string
  name: string
  tag_type?: 'PHYSICAL' | 'LOGICAL'
  data_type?: string
  display_name?: string
  unit?: string
  description?: string
  read_write?: string
  source_type?: string
  source_path?: string
  aggregate_fn?: string
  formula?: string
  formula_type?: string
  sources?: string[]
}

export async function createTag(input: TagCreateInput): Promise<{ status: string; id: string; name: string; tag_type: string }> {
  const res = await apiFetch(`${API_BASE}/tags`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `Create tag failed: ${res.status}`)
  }
  return res.json()
}

export async function importNeuronTags(input: { node_id: string; neuron_node: string; neuron_group: string }): Promise<{ imported: number; skipped: number; total?: number; message?: string }> {
  const res = await apiFetch(`${API_BASE}/tags/import-neuron`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `Import Neuron tags failed: ${res.status}`)
  }
  return res.json()
}

// ── Neuron Proxy API ──

export async function fetchNeuronNodes(): Promise<NeuronNode[]> {
  const res = await apiFetch(`${API_BASE}/neuron/nodes`)
  const data = await res.json()
  return data.nodes || []
}

export async function createNeuronNode(node: {
  name: string
  plugin: string
  host?: string
  port?: number
  device?: string
  baud?: number
}): Promise<any> {
  const res = await apiFetch(`${API_BASE}/neuron/nodes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(node),
  })
  if (!res.ok) throw new Error(`Create node failed: ${res.status}`)
  return res.json()
}

export async function deleteNeuronNode(name: string): Promise<any> {
  const res = await apiFetch(`${API_BASE}/neuron/nodes/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error(`Delete node failed: ${res.status}`)
  return res.json()
}

export async function startNeuronNode(name: string): Promise<any> {
  const res = await apiFetch(`${API_BASE}/neuron/nodes/${encodeURIComponent(name)}/start`, {
    method: 'POST',
  })
  return res.json()
}

export async function stopNeuronNode(name: string): Promise<any> {
  const res = await apiFetch(`${API_BASE}/neuron/nodes/${encodeURIComponent(name)}/stop`, {
    method: 'POST',
  })
  return res.json()
}

export async function fetchNeuronGroups(node: string): Promise<NeuronGroup[]> {
  const res = await apiFetch(`${API_BASE}/neuron/groups?node=${encodeURIComponent(node)}`)
  const data = await res.json()
  return data.groups || []
}

export async function fetchNeuronTags(node: string, group: string): Promise<NeuronTag[]> {
  const res = await apiFetch(`${API_BASE}/neuron/tags?node=${encodeURIComponent(node)}&group=${encodeURIComponent(group)}`)
  const data = await res.json()
  return data.tags || []
}

export async function writeNeuronTag(node: string, group: string, tag: string, value: any): Promise<any> {
  const res = await apiFetch(`${API_BASE}/neuron/write`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ node, group, tag, value }),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `Write tag failed: ${res.status}`)
  }
  return res.json()
}


// ── Category API ──

export async function fetchCategories(): Promise<Category[]> {
  const res = await apiFetch(`${API_BASE}/categories`)
  const data = await res.json()
  return data.categories || []
}

export async function createCategory(category: {
  name: string
  node_type: string
  description?: string
}): Promise<any> {
  const res = await apiFetch(`${API_BASE}/categories`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(category),
  })
  if (!res.ok) throw new Error(`Create category failed: ${res.status}`)
  return res.json()
}

export async function deleteCategory(id: string): Promise<any> {
  const res = await apiFetch(`${API_BASE}/categories/${id}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error(`Delete category failed: ${res.status}`)
  return res.json()
}

export async function fetchTags(
  nodeId?: string,
  page = 1,
  pageSize = 50,
  search?: string,
  dataType?: string,
  tagType?: string,
  readWrite?: string,
  enabled?: boolean,
  sortBy?: string,
  sortOrder?: 'asc' | 'desc',
): Promise<{ tags: Tag[]; total: number; page: number; page_size: number; total_pages: number }> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
  if (nodeId) params.set('node_id', nodeId)
  if (search) params.set('search', search)
  if (dataType) params.set('data_type', dataType)
  if (tagType) params.set('tag_type', tagType)
  if (readWrite) params.set('read_write', readWrite)
  if (enabled !== undefined) params.set('enabled', String(enabled))
  if (sortBy) params.set('sort_by', sortBy)
  if (sortOrder) params.set('sort_order', sortOrder)
  const res = await apiFetch(`${API_BASE}/tags?${params}`)
  return res.json()
}

export async function batchUpdateTags(
  tagIds: string[],
  updates: {
    scale_factor?: number
    value_offset?: number
    unit?: string
    read_write?: string
    enabled?: boolean
    node_id?: string
    alarm_level?: string
    alarm_type?: string
    alarm_threshold?: number
    fault_map_id?: string
  },
): Promise<any> {
  const res = await apiFetch(`${API_BASE}/tags/batch`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag_ids: tagIds, ...updates }),
  })
  if (!res.ok) throw new Error(`Batch update failed: ${res.status}`)
  return res.json()
}

export async function deleteTag(tagId: string): Promise<{ status: string; deleted: string }> {
  const res = await apiFetch(`${API_BASE}/tags/${tagId}`, { method: 'DELETE' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `Delete tag failed: ${res.status}`)
  }
  return res.json()
}

export function exportTagsCsv(nodeId?: string, search?: string): Promise<void> {
  const params = new URLSearchParams()
  if (nodeId) params.set('node_id', nodeId)
  if (search) params.set('search', search)
  return downloadAuthenticated(`${API_BASE}/tags/export?${params}`, 'zizu_tags.csv')
}

export async function updateTag(tagId: string, updates: Partial<Pick<Tag, 'scale_factor' | 'value_offset' | 'unit' | 'display_name' | 'alarm_level' | 'alarm_type' | 'alarm_threshold' | 'fault_map_id'>>): Promise<any> {
  const res = await apiFetch(`${API_BASE}/tags/${tagId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
  if (!res.ok) throw new Error(`Update failed: ${res.status}`)
  return res.json()
}

export async function fetchHealth(): Promise<HealthStatus> {
  const res = await apiFetch(`${API_BASE}/health`)
  return res.json()
}

export interface HistoryPoint {
  ts: string
  raw_value: number | null
  eng_value: number | null
}

export interface HistoryResponse {
  tag_id: string
  tag_name: string
  range: string
  bucket: string
  points: HistoryPoint[]
}

export async function fetchTagHistory(tagId: string, range: '1h' | '24h' | '7d'): Promise<HistoryResponse> {
  const res = await apiFetch(`${API_BASE}/tags/${tagId}/history?range=${range}`)
  if (!res.ok) throw new Error(`History fetch failed: ${res.status}`)
  return res.json()
}

export interface TelemetryPoint {
  ts: string
  tag_id: string
  tag_name: string
  node_name: string
  raw_value: number | null
  eng_value: number | null
  quality: number | null
}

export interface TelemetryResponse {
  points: TelemetryPoint[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

export async function fetchTelemetry(
  tagId?: string,
  range: '1h' | '24h' | '7d' | 'all' = '1h',
  page = 1,
  pageSize = 50,
  nodeId?: string,
): Promise<TelemetryResponse> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize), range })
  if (tagId) params.set('tag_id', tagId)
  if (nodeId) params.set('node_id', nodeId)
  const res = await apiFetch(`${API_BASE}/telemetry?${params}`)
  return res.json()
}

export function exportTelemetryCsv(tagId?: string, range: '1h' | '24h' | '7d' | 'all' = '1h', nodeId?: string): Promise<void> {
  const params = new URLSearchParams({ range })
  if (tagId) params.set('tag_id', tagId)
  if (nodeId) params.set('node_id', nodeId)
  return downloadAuthenticated(`${API_BASE}/telemetry/export?${params}`, 'zizu_telemetry.csv')
}

// ── Admin / Developer API ──

export interface PipelineConfig {
  batch_size: number
  flush_interval_sec: number
}

export async function fetchPipelineConfig(): Promise<PipelineConfig> {
  const res = await apiFetch(`${API_BASE}/pipeline/config`)
  return res.json()
}

export async function updatePipelineConfig(config: PipelineConfig): Promise<any> {
  const res = await apiFetch(`${API_BASE}/pipeline/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  })
  if (!res.ok) throw new Error(`Update failed: ${res.status}`)
  return res.json()
}

export interface MqttConfig {
  mqtt_telemetry_topic: string
  persisted: string | null
  effective_topics: string[]
}

export async function fetchMqttConfig(): Promise<MqttConfig> {
  const res = await apiFetch(`${API_BASE}/mqtt-config`)
  return res.json()
}

export async function updateMqttConfig(config: { mqtt_telemetry_topic: string }): Promise<MqttConfig> {
  const res = await apiFetch(`${API_BASE}/mqtt-config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config),
  })
  if (!res.ok) throw new Error(`Update failed: ${res.status}`)
  return res.json()
}

export interface SqlQueryResult {
  columns: string[]
  rows: any[][]
  row_count: number
  sql: string
}

export async function executeSql(sql: string, limit = 500): Promise<SqlQueryResult> {
  const res = await apiFetch(`${API_BASE}/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sql, limit }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
    throw new Error(err.detail || `Query failed: ${res.status}`)
  }
  return res.json()
}

export async function truncateTable(table: string, confirm: string): Promise<any> {
  const res = await apiFetch(`${API_BASE}/admin/truncate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ table, confirm }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
    throw new Error(err.detail || `Truncate failed: ${res.status}`)
  }
  return res.json()
}


// ── WebSocket ──

export type TelemetryCallback = (updates: TelemetryUpdate[]) => void

export function connectTelemetryWS(
  onMessage: TelemetryCallback,
  tagIds?: string[],
): () => void {
  let ws: WebSocket | null = null
  let cancelled = false

  void (async () => {
    const issued = await apiFetch(`${API_BASE}/auth/ws-ticket`, { method: 'POST' })
    if (!issued.ok || cancelled) return
    const { ticket } = await issued.json() as { ticket: string }
    if (cancelled) return
    ws = new WebSocket(WS_URL)

    ws.onopen = () => {
      ws?.send(JSON.stringify({ authenticate: { ticket } }))
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'authenticated') {
          ws?.send(JSON.stringify({ subscribe: tagIds || [] }))
        } else if (data.tags && Array.isArray(data.tags)) {
          onMessage(data.tags)
        }
      } catch {
        // ignore parse errors
      }
    }

    ws.onerror = (err) => {
      console.error('[WS] Error:', err)
    }
  })().catch((err) => console.error('[WS] Ticket failed:', err))

  return () => {
    cancelled = true
    ws?.close()
  }
}

// ── Node Config Update ──

export interface NodeUpdateRequest {
  name?: string
  node_type?: string
  sort_order?: number
  enabled?: boolean
  config?: Record<string, any>
}

export async function updateNode(nodeId: string, updates: NodeUpdateRequest): Promise<Node> {
  const res = await apiFetch(`${API_BASE}/nodes/${nodeId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
  if (!res.ok) throw new Error(`Update node failed: ${res.status}`)
  const data = await res.json()
  return data.node || data
}

// ── Rules ──

export interface Rule {
  id: string
  name: string
  rule_type: 'alarm' | 'control' | 'fault_map' | 'linkage'
  jdm_content: Record<string, any>
  version: number
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface RuleCreateRequest {
  name: string
  rule_type: Rule['rule_type']
  jdm_content?: Record<string, any>
  enabled?: boolean
}

export async function fetchRules(): Promise<Rule[]> {
  const res = await apiFetch(`${API_BASE}/rules`)
  const data = await res.json()
  return data.rules || []
}

export async function fetchRule(ruleId: string): Promise<Rule> {
  const res = await apiFetch(`${API_BASE}/rules/${ruleId}`)
  if (!res.ok) throw new Error(`Fetch rule failed: ${res.status}`)
  return res.json()
}

export async function createRule(rule: RuleCreateRequest): Promise<Rule> {
  const res = await apiFetch(`${API_BASE}/rules`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(rule),
  })
  if (!res.ok) throw new Error(`Create rule failed: ${res.status}`)
  return res.json()
}

export async function updateRule(ruleId: string, updates: Partial<RuleCreateRequest>): Promise<Rule> {
  const res = await apiFetch(`${API_BASE}/rules/${ruleId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
  if (!res.ok) throw new Error(`Update rule failed: ${res.status}`)
  return res.json()
}

export async function deleteRule(ruleId: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/rules/${ruleId}`, {
    method: 'DELETE',
  })
  if (!res.ok) throw new Error(`Delete rule failed: ${res.status}`)
}

export async function simulateRule(ruleId: string, context: Record<string, any>): Promise<any> {
  const res = await apiFetch(`${API_BASE}/rules/${ruleId}/simulate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ context }),
  })
  if (!res.ok) throw new Error(`Simulate rule failed: ${res.status}`)
  return res.json()
}

export async function evaluateGraph(graph: Record<string, any>, context: Record<string, any>): Promise<any> {
  const res = await apiFetch(`${API_BASE}/rules/evaluate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: graph, context }),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || `Evaluate graph failed: ${res.status}`)
  }
  return res.json()
}

// ── Rule Templates ──

export interface RuleTemplate {
  id: string
  name: string
  description: string | null
  rule_type: Rule['rule_type']
  graph: Record<string, any>
  config: Record<string, any>
  enabled: boolean
  is_default: boolean
  created_at: string
  updated_at: string
}

export async function fetchRuleTemplates(): Promise<RuleTemplate[]> {
  const res = await apiFetch(`${API_BASE}/rule-templates`)
  if (!res.ok) throw new Error(`Fetch templates failed: ${res.status}`)
  const data = await res.json()
  return data.templates || []
}

export async function fetchRuleTemplate(templateId: string): Promise<RuleTemplate> {
  const res = await apiFetch(`${API_BASE}/rule-templates/${templateId}`)
  if (!res.ok) throw new Error(`Fetch template failed: ${res.status}`)
  return res.json()
}

export interface RuleTemplateCreateInput {
  name: string
  description?: string | null
  rule_type: Rule['rule_type']
  graph?: Record<string, any>
  config?: Record<string, any>
  enabled?: boolean
}

export async function createRuleTemplate(input: RuleTemplateCreateInput): Promise<RuleTemplate> {
  const res = await apiFetch(`${API_BASE}/rule-templates`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw new Error(`Create template failed: ${res.status}`)
  return res.json()
}

export async function updateRuleTemplate(templateId: string, updates: Partial<RuleTemplateCreateInput>): Promise<RuleTemplate> {
  const res = await apiFetch(`${API_BASE}/rule-templates/${templateId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
  if (!res.ok) throw new Error(`Update template failed: ${res.status}`)
  return res.json()
}

export async function deleteRuleTemplate(templateId: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/rule-templates/${templateId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Delete template failed: ${res.status}`)
}

export async function applyRuleTemplate(templateId: string, name: string, enabled = true): Promise<{ rule: any; status: string }> {
  const res = await apiFetch(`${API_BASE}/rule-templates/${templateId}/apply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, enabled }),
  })
  if (!res.ok) throw new Error(`Apply template failed: ${res.status}`)
  return res.json()
}

// ── Alarms ──

export interface TriggerRule {
  op: 'active' | 'eq' | 'ne' | 'gte' | 'gt' | 'lte' | 'lt' | 'fault'
  value?: string | number | null
  threshold?: number | null
  fault_map_id?: string | null
}

export interface AlarmLevelEntity {
  id: string
  code: string
  name: string
  severity: 'INFO' | 'WARNING' | 'MAJOR' | 'CRITICAL'
  color: string | null
  trigger_rules: TriggerRule[]
  enabled: boolean
  sort_order: number
  is_system: boolean
  created_at: string
  updated_at: string
}

export type AlarmLevel = 'INFO' | 'WARNING' | 'MAJOR' | 'CRITICAL' 

export interface Alarm {
  id: string
  rule_id: string | null
  rule_name?: string
  node_id: string | null
  node_name?: string
  entity_id?: string | null
  entity_name?: string
  entity_is_system?: boolean
  level: AlarmLevel
  message: string
  acknowledged: boolean
  ack_user: string | null
  ack_at: string | null
  created_at: string
  resolved_at: string | null
  source_topic?: string | null
  source_key?: string | null
  external_id?: string | null
  alarm_type?: string | null
  alarm_threshold?: number | null
  alarm_source?: string | null
  alarm_count?: number | null
  alarm_code?: string | null
  state?: 'pending' | 'active_unacknowledged' | 'active_acknowledged' | 'recovered'
}

export interface AlarmListResponse {
  alarms: Alarm[]
  total: number
  page: number
  page_size: number
  total_pages: number
  summary: {
    total: number
    unacknowledged: number
    by_severity: Record<AlarmLevel, number>
  }
}

interface AlarmEventWire {
  id: string
  definition_id: string
  entity_instance_id: string
  state: 'pending' | 'active_unacknowledged' | 'active_acknowledged' | 'recovered'
  severity: AlarmLevel
  pending_at: string
  active_at: string | null
  acknowledged_at: string | null
  acknowledged_by: string | null
  recovered_at: string | null
  last_observation: { evidence?: { alarm_definition?: string } } | null
}

export async function fetchAlarms(
  page = 1,
  pageSize = 50,
  level?: AlarmLevel,
  sourceKey?: string,
  acknowledged?: boolean,
  resolved?: boolean,
  nodeId?: string,
  entityId?: string,
): Promise<AlarmListResponse> {
  void sourceKey
  void nodeId
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
  if (level) params.set('severity', level)
  if (entityId) params.set('entity_instance_id', entityId)
  if (resolved === true) params.set('state', 'recovered')
  else if (acknowledged === true) params.set('state', 'active_acknowledged')
  else if (acknowledged === false && resolved === false) params.set('state', 'open')
  const res = await apiFetch(`${API_BASE}/alarm-events?${params}`)
  if (!res.ok) throw new Error(`Fetch alarm events failed: ${res.status}`)
  const data: {
    items: AlarmEventWire[]
    total: number
    page: number
    page_size: number
    total_pages: number
    summary: AlarmListResponse['summary']
  } = await res.json()
  return {
    alarms: data.items.map((item): Alarm => ({
      id: item.id,
      rule_id: null,
      node_id: null,
      entity_id: item.entity_instance_id,
      entity_name: `实体实例 ${item.entity_instance_id}`,
      level: item.severity,
      message: item.last_observation?.evidence?.alarm_definition || `告警定义 ${item.definition_id}`,
      acknowledged: item.state === 'active_acknowledged',
      ack_user: item.acknowledged_by,
      ack_at: item.acknowledged_at,
      created_at: item.active_at || item.pending_at,
      resolved_at: item.recovered_at,
      state: item.state,
    })),
    total: data.total,
    page: data.page,
    page_size: data.page_size,
    total_pages: data.total_pages,
    summary: data.summary,
  }
}



export async function fetchAlarmEntities(): Promise<{ items: { id: string; name: string; display_name: string | null }[] }> {
  const res = await apiFetch(`${API_BASE}/alarms/entities`)
  if (!res.ok) throw new Error(`Fetch alarm entities failed: ${res.status}`)
  return res.json()
}
export async function fetchAlarmGroupCounts(): Promise<Record<string, number>> {
  const res = await apiFetch(`${API_BASE}/alarms/group-counts`)
  if (!res.ok) throw new Error(`Fetch alarm group counts failed: ${res.status}`)
  const data = await res.json()
  return data.counts || {}
}

export async function fetchAlarmCounts(nodeIds?: string[]): Promise<Record<string, number>> {
  const params = new URLSearchParams()
  if (nodeIds && nodeIds.length > 0) params.set('node_ids', nodeIds.join(','))
  const res = await apiFetch(`${API_BASE}/alarms/counts?${params}`)
  if (!res.ok) throw new Error(`Fetch alarm counts failed: ${res.status}`)
  const data = await res.json()
  return data.counts || {}
}

export async function acknowledgeAlarm(alarmId: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/alarm-events/${alarmId}/acknowledgements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
  if (!res.ok) throw new Error(`Acknowledge alarm failed: ${res.status}`)
}
// ── Alarm Types & Config ──

export async function fetchAlarmTypes(): Promise<string[]> {
  const res = await apiFetch(`${API_BASE}/alarms/alarm-types`)
  if (!res.ok) throw new Error(`Fetch alarm types failed: ${res.status}`)
  const data = await res.json()
  return data.types || []
}

export interface AlarmConfigTag {
  id: string
  name: string
  display_name: string | null
  node_id: string
  node_name: string
  node_type: string
  alarm_level: string
  alarm_type: string | null
  alarm_threshold: number | null
  fault_map_id: string | null
  fault_map_name: string | null
}

export async function fetchAlarmConfig(): Promise<{ tags: AlarmConfigTag[]; total: number }> {
  const res = await apiFetch(`${API_BASE}/tags/alarm-config`)
  if (!res.ok) throw new Error(`Fetch alarm config failed: ${res.status}`)
  return res.json()
}

// ── Global Entities ──

export interface EntityInstance {
  id: string
  device_instance_id: string
  slot_id: string
  instance_key: string
  device_category: string
  device_display_name: string
  definition_id: string
  display_name: string
  data_type: string
  unit: string | null
  direction: 'R' | 'W' | 'RW'
  freshness_seconds: number
}

export interface ControlCommand {
  id: string
  status: string
  code: string
  source_type: string
}

export interface ControlConfirmation {
  id: string
  expires_at: string
}

export interface WorkbenchEntity {
  entity_instance_id: string
  slot_id: string
  instance_key: string
  definition_id: string
  display_name: string
  data_type: 'float' | 'int' | 'bool' | 'string' | string
  unit: string | null
  direction: 'R' | 'W' | 'RW'
  status?: 'available' | 'unavailable'
  code?: string
  value?: unknown
  observed_at?: string
  quality?: number
}

export interface EmsWorkbench {
  workbench_id: string
  site_configuration_version: number
  navigation: { id: 'overview' | 'trends' | 'alarms' | 'controls'; label: string }[]
  groups: { id: string; label: string; entities: WorkbenchEntity[] }[]
  kpis: { id: string; label: string; entities: WorkbenchEntity[] }[]
  trends: { id: string; label: string; default_range: '1h' | '24h' | '7d' | '30d'; entities: WorkbenchEntity[] }[]
  alarms: { visible: boolean }
  controls: { visible: boolean; entities: WorkbenchEntity[] }
}

export interface EmsWorkbenchTrend {
  id: string
  label: string
  range: '1h' | '24h' | '7d' | '30d'
  series: (WorkbenchEntity & { points: { ts: string; value: unknown; quality: number }[] })[]
}

export async function fetchEmsWorkbench(): Promise<EmsWorkbench> {
  const response = await apiFetch(`${API_BASE}/ems-workbench`)
  if (!response.ok) throw await authError(response, `Fetch EMS workbench failed: ${response.status}`)
  return response.json()
}

export async function fetchEmsWorkbenchTrend(
  trendId: string,
  range: EmsWorkbenchTrend['range'],
): Promise<EmsWorkbenchTrend> {
  const response = await apiFetch(`${API_BASE}/ems-workbench/trends/${encodeURIComponent(trendId)}?range=${range}`)
  if (!response.ok) throw await authError(response, `Fetch EMS trend failed: ${response.status}`)
  return response.json()
}

export async function requestControlConfirmation(
  entityInstanceId: string,
  value: unknown,
): Promise<ControlConfirmation> {
  const response = await apiFetch(`${API_BASE}/entity-instances/${entityInstanceId}/control-confirmations`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': crypto.randomUUID(),
    },
    body: JSON.stringify({ value }),
  })
  if (!response.ok) throw await authError(response, `Control confirmation failed: ${response.status}`)
  return response.json()
}

export async function submitControlCommand(
  entityInstanceId: string,
  value: unknown,
  confirmationId?: string,
): Promise<ControlCommand> {
  const response = await apiFetch(`${API_BASE}/entity-instances/${entityInstanceId}/control-commands`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': crypto.randomUUID(),
    },
    body: JSON.stringify({ value, ...(confirmationId ? { confirmation_id: confirmationId } : {}) }),
  })
  if (!response.ok) throw await authError(response, `Control command failed: ${response.status}`)
  return response.json()
}

export async function fetchControlCommand(commandId: string): Promise<ControlCommand> {
  const response = await apiFetch(`${API_BASE}/control-commands/${encodeURIComponent(commandId)}`)
  if (!response.ok) throw await authError(response, `Fetch control command failed: ${response.status}`)
  return response.json()
}

export async function reconcileControlCommand(commandId: string): Promise<ControlCommand> {
  const response = await apiFetch(`${API_BASE}/control-commands/${encodeURIComponent(commandId)}/reconcile`, {
    method: 'POST',
  })
  if (!response.ok) throw await authError(response, `Reconcile control command failed: ${response.status}`)
  return response.json()
}

export interface SolutionPackage {
  id: string
  package_id: string
  version: string
  display_name: string
  digest: string
  status: string
  parameter_contracts: { id: string; type: string; required?: boolean; description?: string; values?: string[] }[]
}

export interface InstallationPlan {
  id: string
  package_record_id: string
  status: string
  digest: string
  blockers: { code: string; message: string }[]
  items: Record<string, unknown>[]
}

export interface SolutionInstallation {
  id: string
  package_record_id: string
  site_configuration_version: number
  status: string
}

export interface DeliveryReport {
  id: string
  installation_id: string
  status: string
  items: { acceptance_id: string; status: string; code: string }[]
}

export async function fetchSolutionPackages(): Promise<SolutionPackage[]> {
  const response = await apiFetch(`${API_BASE}/solution-packages`)
  if (!response.ok) throw await authError(response, `Fetch solution packages failed: ${response.status}`)
  return (await response.json() as { items: SolutionPackage[] }).items
}

export async function importSolutionPackage(archive: File): Promise<SolutionPackage> {
  const data = new FormData()
  data.append('archive', archive)
  const response = await apiFetch(`${API_BASE}/solution-packages/import`, { method: 'POST', body: data })
  if (!response.ok) throw await authError(response, `Import solution package failed: ${response.status}`)
  return response.json()
}

export async function createInstallationPlan(
  packageRecordId: string,
  request: { parameters: Record<string, unknown>; secret_references: Record<string, string> },
): Promise<InstallationPlan> {
  const response = await apiFetch(`${API_BASE}/solution-packages/${encodeURIComponent(packageRecordId)}/install-plans`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
  if (!response.ok) throw await authError(response, `Create installation plan failed: ${response.status}`)
  return response.json()
}

export async function applyInstallationPlan(plan: InstallationPlan): Promise<SolutionInstallation> {
  const response = await apiFetch(`${API_BASE}/install-plans/${encodeURIComponent(plan.id)}/apply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
    body: JSON.stringify({ plan_digest: plan.digest }),
  })
  if (!response.ok) throw await authError(response, `Apply installation plan failed: ${response.status}`)
  return response.json()
}

export async function fetchSolutionInstallations(): Promise<SolutionInstallation[]> {
  const response = await apiFetch(`${API_BASE}/solution-installations`)
  if (!response.ok) throw await authError(response, `Fetch solution installations failed: ${response.status}`)
  return (await response.json() as { items: SolutionInstallation[] }).items
}

export interface DeliveryAcceptanceInput {
  manual_commands?: Record<string, string>
  policy_commands?: Record<string, string>
  authorization_denials?: Record<string, string>
}

export async function runDeliveryAcceptance(
  installationId: string,
  input: DeliveryAcceptanceInput = {},
): Promise<DeliveryReport> {
  const response = await apiFetch(`${API_BASE}/solution-installations/${encodeURIComponent(installationId)}/acceptance-runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
    body: JSON.stringify(input),
  })
  if (!response.ok) throw await authError(response, `Run delivery acceptance failed: ${response.status}`)
  return response.json()
}

export interface LegacyEntityMigrationItem {
  legacy_entity_id: string
  legacy_entity_name: string
  classification: 'unique' | 'missing' | 'ambiguous'
  candidate_entity_instance_ids: string[]
}

export async function fetchEntityInstances(): Promise<{ items: EntityInstance[]; total: number }> {
  const res = await apiFetch(`${API_BASE}/entity-instances`)
  if (!res.ok) throw new Error(`Fetch entity instances failed: ${res.status}`)
  return res.json()
}

export async function previewLegacyEntityMigration(): Promise<{
  items: LegacyEntityMigrationItem[]
  counts: Record<'unique' | 'missing' | 'ambiguous', number>
  writes_applied: 0
}> {
  const res = await apiFetch(`${API_BASE}/entity-instances/legacy-migration-preview`)
  if (!res.ok) throw new Error(`Preview legacy entity migration failed: ${res.status}`)
  return res.json()
}

export interface Entity {
  id: string
  name: string
  display_name: string | null
  entity_type: 'R' | 'W' | 'RW'
  data_type: string
  unit: string | null
  category: string | null
  description: string | null
  enabled: boolean
  binding_count: number
  is_system?: boolean
  std_field?: string | null
  std_ref?: string | null
}

export interface EntityAlarmBinding {
  id: string
  entity_id: string
  entity_name: string
  entity_display_name: string | null
  alarm_level_id: string
  trigger_rules: TriggerRule[]
  fault_map_id: string | null
  fault_map_name: string | null
  enabled: boolean
  created_at: string
}

export interface EntityBinding {
  id: string
  entity_id: string
  tag_id: string
  node_id: string
  binding_type: 'PHYSICAL' | 'VIRTUAL'
  brand: string | null
  priority: number
  enabled: boolean
  tag_name?: string
  tag_display_name?: string

  node_name?: string
  entity_is_system?: boolean
}

 export interface EntityRealtime {
  entity_id: string
  entity_name: string
  entity_display_name: string | null
  value: number | string | boolean | null
  ts: string | null
  unit: string | null
  tag_id: string
  tag_name: string
  node_id: string
  node_name: string
}

export async function fetchEntities(params?: { category?: string; entity_type?: string; search?: string; enabled?: boolean; page?: number; page_size?: number }): Promise<{ items: Entity[]; total: number; page: number; page_size: number; total_pages: number }> {
  const qs = new URLSearchParams()
  if (params?.category) qs.set('category', params.category)
  if (params?.entity_type) qs.set('entity_type', params.entity_type)
  if (params?.search) qs.set('search', params.search)
  if (params?.enabled !== undefined) qs.set('enabled', String(params.enabled))
  qs.set('page', String(params?.page || 1))
  qs.set('page_size', String(params?.page_size || 50))
  const res = await apiFetch(`${API_BASE}/entities?${qs}`)
  if (!res.ok) throw new Error(`Fetch entities failed: ${res.status}`)
  return res.json()
}

export async function fetchEntity(entityId: string): Promise<Entity & { bindings: EntityBinding[] }> {
  const res = await apiFetch(`${API_BASE}/entities/${entityId}`)
  if (!res.ok) throw new Error(`Fetch entity failed: ${res.status}`)
  return res.json()
}

export async function createEntity(input: Omit<Entity, 'id' | 'binding_count'>): Promise<{ id: string; created_at: string }> {
  const res = await apiFetch(`${API_BASE}/entities`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw new Error(`Create entity failed: ${res.status}`)
  return res.json()
}

export async function updateEntity(entityId: string, input: Partial<Omit<Entity, 'id'>>): Promise<{ updated: boolean; updated_at?: string }> {
  const res = await apiFetch(`${API_BASE}/entities/${entityId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw new Error(`Update entity failed: ${res.status}`)
  return res.json()
}

export async function deleteEntity(entityId: string): Promise<{ deleted: boolean }> {
  const res = await apiFetch(`${API_BASE}/entities/${entityId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Delete entity failed: ${res.status}`)
  return res.json()
}

export async function bindTagToEntity(entityId: string, input: Omit<EntityBinding, 'id' | 'entity_id' | 'tag_name' | 'tag_display_name' | 'node_name'>): Promise<{ id: string; created_at: string }> {
  const res = await apiFetch(`${API_BASE}/entities/${entityId}/bindings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw new Error(`Bind tag failed: ${res.status}`)
  return res.json()
}

export async function unbindTagFromEntity(entityId: string, bindingId: string): Promise<{ deleted: boolean }> {
  const res = await apiFetch(`${API_BASE}/entities/${entityId}/bindings/${bindingId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Unbind tag failed: ${res.status}`)
  return res.json()
}

export async function fetchEntityRealtime(entityId: string): Promise<EntityRealtime> {
  const res = await apiFetch(`${API_BASE}/entities/${entityId}/realtime`)
  if (!res.ok) throw new Error(`Fetch entity realtime failed: ${res.status}`)
  return res.json()
}

export async function fetchEntitiesByNode(nodeId: string): Promise<{ items: Entity[]; total: number }> {
  const qs = new URLSearchParams()
  qs.set('node_id', nodeId)
  qs.set('page', '1')
  qs.set('page_size', '200')
  const res = await apiFetch(`${API_BASE}/entities?${qs}`)
  if (!res.ok) throw new Error(`Fetch entities by node failed: ${res.status}`)
  return res.json()
}
export async function fetchEntityBindings(params?: { node_id?: string; entity_id?: string }): Promise<{ bindings: (EntityBinding & { entity_name?: string; entity_display_name?: string; entity_type?: string; data_type?: string; unit?: string | null })[]; total: number }> {
  const qs = new URLSearchParams()
  if (params?.node_id) qs.set('node_id', params.node_id)
  if (params?.entity_id) qs.set('entity_id', params.entity_id)
  const res = await apiFetch(`${API_BASE}/entities/bindings?${qs}`)
  if (!res.ok) throw new Error(`Fetch entity bindings failed: ${res.status}`)
  return res.json()
}

export interface BatchBindingItem {
  entity_id: string
  tag_id: string
  node_id: string
  binding_type: 'PHYSICAL' | 'VIRTUAL'
  brand?: string | null
  priority?: number
  enabled?: boolean
}

export async function batchBindEntityTags(bindings: BatchBindingItem[]): Promise<{ created: number; skipped: number; total: number }> {
  const res = await apiFetch(`${API_BASE}/entities/bindings/batch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bindings }),
  })
  if (!res.ok) throw new Error(`Batch bind failed: ${res.status}`)
  return res.json()
}

export async function batchUnbindEntityBindings(bindingIds: string[]): Promise<{ deleted: number }> {
  const res = await apiFetch(`${API_BASE}/entities/bindings/batch`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ binding_ids: bindingIds }),
  })
  if (!res.ok) throw new Error(`Batch unbind failed: ${res.status}`)
  return res.json()
}


export async function fetchEntityHistory(entityId: string, range = '1h', page = 1, pageSize = 500): Promise<{ points: { ts: string; value: number | string | boolean | null; quality: number }[]; total: number; page: number; page_size: number }> {
  const res = await apiFetch(`${API_BASE}/entities/${entityId}/history?range=${range}&page=${page}&page_size=${pageSize}`)
  if (!res.ok) throw new Error(`Fetch entity history failed: ${res.status}`)
  return res.json()
}

export function exportEntitiesCsv(category?: string): Promise<void> {
  const params = new URLSearchParams()
  if (category) params.set('category', category)
  params.set('format', 'csv')
  return downloadAuthenticated(`${API_BASE}/entities/export?${params}`, 'zizu_entities.csv')
}

export function exportEntitiesJson(category?: string): Promise<void> {
  const params = new URLSearchParams()
  if (category) params.set('category', category)
  params.set('format', 'json')
  return downloadAuthenticated(`${API_BASE}/entities/export?${params}`, 'zizu_entities.json')
}

export interface EntityImportResult {
  created: number
  updated: number
  skipped: number
  errors: string[]
  dry_run: boolean
  total: number
}

export async function importEntitiesFile(file: File, mode: 'upsert' | 'create' = 'upsert', dryRun = false): Promise<EntityImportResult> {
  const text = await file.text()
  const params = new URLSearchParams({ mode, dry_run: String(dryRun) })
  const isJson = file.name.toLowerCase().endsWith('.json') || text.trim().startsWith('[') || text.trim().startsWith('{')
  const res = await apiFetch(`${API_BASE}/entities/import?${params}`, { method: 'POST', headers: { 'Content-Type': isJson ? 'application/json' : 'text/csv' }, body: text })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `Import entities failed: ${res.status}`)
  }
  return res.json()
}



export async function autoBindEntities(dryRun = false): Promise<{ created: number; skipped: number; preview: { entity_name: string; tag_name: string; node_name: string; node_type: string }[] }> {
  const res = await apiFetch(`${API_BASE}/entities/bindings/auto-bind?dry_run=${dryRun}`, { method: 'POST' })
  if (!res.ok) throw new Error(`Auto bind failed: ${res.status}`)
  return res.json()
}




// ── Fault Map API ──

export interface FaultMapEntry {
  code: string
  message: string
}

export interface FaultMap {
  id: string
  name: string
  description: string | null
  entries: FaultMapEntry[]
  created_at: string
  updated_at: string
}



export async function fetchAlarmLevels(enabledOnly = false): Promise<{ items: AlarmLevelEntity[] }> {
  const res = await apiFetch(`${API_BASE}/alarm-levels?enabled_only=${enabledOnly}`)
  if (!res.ok) throw new Error(`Fetch alarm levels failed: ${res.status}`)
  return res.json()
}

export async function createAlarmLevel(data: Omit<AlarmLevelEntity, 'id' | 'created_at' | 'updated_at' | 'is_system'>): Promise<{ id: string; created_at: string }> {
  const res = await apiFetch(`${API_BASE}/alarm-levels`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!res.ok) throw new Error(`Create alarm level failed: ${res.status}`)
  return res.json()
}

export async function updateAlarmLevel(levelId: string, data: Partial<Omit<AlarmLevelEntity, 'id' | 'created_at' | 'updated_at' | 'is_system'>>): Promise<{ status: string }> {
  const res = await apiFetch(`${API_BASE}/alarm-levels/${levelId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!res.ok) throw new Error(`Update alarm level failed: ${res.status}`)
  return res.json()
}

export async function deleteAlarmLevel(levelId: string): Promise<{ deleted: boolean }> {
  const res = await apiFetch(`${API_BASE}/alarm-levels/${levelId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Delete alarm level failed: ${res.status}`)
  return res.json()
}

export async function fetchAlarmLevelEntities(levelId: string): Promise<{ items: EntityAlarmBinding[] }> {
  const res = await apiFetch(`${API_BASE}/alarm-levels/${levelId}/entities`)
  if (!res.ok) throw new Error(`Fetch alarm level entities failed: ${res.status}`)
  return res.json()
}

export async function batchBindEntitiesToAlarmLevel(
  levelId: string,
  entityIds: string[],
  triggerRules?: TriggerRule[],
  enabled = true,
  faultMapId?: string | null,
): Promise<{ bound: number }> {
  const res = await apiFetch(`${API_BASE}/alarm-levels/${levelId}/entities`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entity_ids: entityIds, trigger_rules: triggerRules, enabled, fault_map_id: faultMapId }),
  })
  if (!res.ok) throw new Error(`Batch bind failed: ${res.status}`)
  return res.json()
}

export async function unbindEntityFromAlarmLevel(levelId: string, bindingId: string): Promise<{ deleted: boolean }> {
  const res = await apiFetch(`${API_BASE}/alarm-levels/${levelId}/entities/${bindingId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Unbind failed: ${res.status}`)
  return res.json()
}

export async function fetchFaultMaps(): Promise<{ items: FaultMap[]; total: number }> {
  const res = await apiFetch(`${API_BASE}/fault-maps`)
  return res.json()
}

export async function createFaultMap(data: Omit<FaultMap, 'id' | 'created_at' | 'updated_at'>): Promise<{ id: string; status: string }> {
  const res = await apiFetch(`${API_BASE}/fault-maps`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!res.ok) throw new Error(`Create fault map failed: ${res.status}`)
  return res.json()
}

export async function updateFaultMap(mapId: string, data: Partial<Omit<FaultMap, 'id' | 'created_at' | 'updated_at'>>): Promise<{ status: string }> {
  const res = await apiFetch(`${API_BASE}/fault-maps/${mapId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!res.ok) throw new Error(`Update fault map failed: ${res.status}`)
  return res.json()
}

export async function deleteFaultMap(mapId: string): Promise<{ status: string }> {
  const res = await apiFetch(`${API_BASE}/fault-maps/${mapId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Delete fault map failed: ${res.status}`)
  return res.json()
}

// ── nanoMQ 管理 API ──

export interface NanoMQStatus {
  brokers?: any
  nodes?: any
  metrics?: any
  error?: boolean
  status_code?: number
  message?: any
}

export interface NanoMQClientInfo {
  client_id: string
  username?: string
  ipaddress?: string
  port?: number
  proto_name?: string
  connected_at?: string
  keepalive?: number
}

export interface NanoMQSubscription {
  clientid: string
  topic: string
  qos: number
}

export interface NanoMQACLRule {
  action: 'pub' | 'sub' | 'all'
  permit: 'allow' | 'deny'
  username?: string
  clientid?: string
  ipaddr?: string
  topic: string
}

export async function fetchNanoMQStatus(): Promise<NanoMQStatus> {
  const res = await apiFetch(`${API_BASE}/nanomq/status`)
  return res.json()
}

export async function fetchNanoMQClients(): Promise<{ data?: NanoMQClientInfo[]; [k: string]: any }> {
  const res = await apiFetch(`${API_BASE}/nanomq/clients`)
  if (!res.ok) throw new Error(`Fetch clients failed: ${res.status}`)
  return res.json()
}

export async function fetchNanoMQSubscriptions(): Promise<{ data?: NanoMQSubscription[]; [k: string]: any }> {
  const res = await apiFetch(`${API_BASE}/nanomq/subscriptions`)
  if (!res.ok) throw new Error(`Fetch subscriptions failed: ${res.status}`)
  return res.json()
}

export async function fetchNanoMQACL(): Promise<{ data?: NanoMQACLRule[]; [k: string]: any }> {
  const res = await apiFetch(`${API_BASE}/nanomq/acl`)
  if (!res.ok) throw new Error(`Fetch ACL failed: ${res.status}`)
  return res.json()
}

export async function updateNanoMQACL(rules: NanoMQACLRule[]): Promise<{ [k: string]: any }> {
  const res = await apiFetch(`${API_BASE}/nanomq/acl`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rules }),
  })
  if (!res.ok) throw new Error(`Update ACL failed: ${res.status}`)
  return res.json()
}

export async function fetchNanoMQConfig(): Promise<{ content: string; path: string }> {
  const res = await apiFetch(`${API_BASE}/nanomq/config`)
  if (!res.ok) throw new Error(`Fetch config failed: ${res.status}`)
  return res.json()
}

export async function updateNanoMQConfig(content: string): Promise<{ saved: boolean; path: string }> {
  const res = await apiFetch(`${API_BASE}/nanomq/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
  if (!res.ok) throw new Error(`Update config failed: ${res.status}`)
  return res.json()
}

export async function restartNanoMQ(): Promise<{ restarted: boolean; message: string }> {
  const res = await apiFetch(`${API_BASE}/nanomq/restart`, { method: 'POST' })
  if (!res.ok) throw new Error(`Restart failed: ${res.status}`)
  return res.json()
}
// ── Device Templates ──

export interface DeviceTemplate {
  id: string
  name: string
  category: string | null
  description: string | null
  content: { nodes?: DeviceTemplateNode[] }
  is_system?: boolean
  enabled: boolean
  created_at?: string
  updated_at?: string
}

export interface DeviceTemplateNode {
  name: string
  node_type?: string
  config?: Record<string, any>
  sort_order?: number
  tags?: DeviceTemplateTag[]
  children?: DeviceTemplateNode[]
}

export interface DeviceTemplateTag {
  name: string
  display_name?: string
  data_type?: string
  tag_type?: 'PHYSICAL' | 'LOGICAL'
  unit?: string
  read_write?: string
  source_path?: string
  scale_factor?: number
  value_offset?: number
  description?: string
  entity_name?: string
  binding_type?: 'PHYSICAL' | 'VIRTUAL'
}

export async function fetchDeviceTemplates(): Promise<{ items: DeviceTemplate[] }> {
  const res = await apiFetch(`${API_BASE}/device-templates`)
  if (!res.ok) throw new Error(`Fetch templates failed: ${res.status}`)
  return res.json()
}

export async function fetchDeviceTemplate(id: string): Promise<DeviceTemplate> {
  const res = await apiFetch(`${API_BASE}/device-templates/${id}`)
  if (!res.ok) throw new Error(`Fetch template failed: ${res.status}`)
  return res.json()
}

export async function createDeviceTemplate(data: Omit<DeviceTemplate, 'id' | 'created_at' | 'updated_at'>): Promise<DeviceTemplate> {
  const res = await apiFetch(`${API_BASE}/device-templates`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!res.ok) throw new Error(`Create template failed: ${res.status}`)
  return res.json()
}

export async function updateDeviceTemplate(id: string, data: Partial<Omit<DeviceTemplate, 'id' | 'created_at' | 'updated_at'>>): Promise<{ updated: boolean }> {
  const res = await apiFetch(`${API_BASE}/device-templates/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  if (!res.ok) throw new Error(`Update template failed: ${res.status}`)
  return res.json()
}

export async function deleteDeviceTemplate(id: string): Promise<{ deleted: boolean }> {
  const res = await apiFetch(`${API_BASE}/device-templates/${id}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`Delete template failed: ${res.status}`)
  return res.json()
}

export async function applyDeviceTemplate(
  id: string,
  input: { parent_node_id: string; instance_name?: string; source_prefix?: string; brand?: string }
): Promise<{ status: string; summary: { nodes_created: number; tags_created: number; bindings_created: number; entity_missing: string[]; warnings: string[] } }> {
  const res = await apiFetch(`${API_BASE}/device-templates/${id}/apply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) throw new Error(`Apply template failed: ${res.status}`)
  return res.json()
}
