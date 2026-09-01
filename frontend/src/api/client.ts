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
import {
  AlarmConfigurationResultUnknownError,
  readAlarmConfigurationApplyResult,
  type AlarmConditionValue,
} from '../components/alarm-configuration/alarmConfigurationContracts'

export { AlarmConfigurationResultUnknownError } from '../components/alarm-configuration/alarmConfigurationContracts'
export type { AlarmConditionValue } from '../components/alarm-configuration/alarmConfigurationContracts'

const API_BASE = '/api/v1'

/**
 * Single HTTP seam for the frontend. Authentication is resolved at request
 * time so a restored or replaced session is used without rebuilding clients.
 */
export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
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
  wire_data_type: string | null
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
  node_type: string
  config?: Record<string, any>
  sort_order?: number
}

export async function createNode(input: NodeCreateInput): Promise<{ node: Node }> {
  const res = await apiFetch(`${API_BASE}/nodes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail?.message || err.detail || `Create node failed: ${res.status}`)
  }
  return res.json()
}

export async function deleteNode(nodeId: string): Promise<{ retired: string; retired_nodes: number; configuration_revision: number }> {
  const res = await apiFetch(`${API_BASE}/nodes/${nodeId}`, { method: 'DELETE' })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail?.message || err.detail || `Delete node failed: ${res.status}`)
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

export interface NeuronImportItem {
  source_path: string
  group: string
  name: string
  source_address: string
  wire_data_type: string
  value_data_type: string
  action: 'create' | 'update' | 'unchanged' | 'conflict'
  reason?: string | null
}

export interface NeuronImportPreview {
  node_id: string
  neuron_node: string
  selected_groups: string[]
  base_configuration_revision: number
  preview_digest: string
  counts: Partial<Record<NeuronImportItem['action'], number>>
  has_conflicts: boolean
  items: NeuronImportItem[]
}

export interface NeuronImportSelection {
  node_id: string
  neuron_node: string
  neuron_groups: string[]
}

export async function previewNeuronTags(input: NeuronImportSelection): Promise<NeuronImportPreview> {
  const res = await apiFetch(`${API_BASE}/tags/import-neuron/preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail?.message || err.detail || `Preview Neuron tags failed: ${res.status}`)
  }
  return res.json()
}

export async function importNeuronTags(input: NeuronImportSelection & { preview_digest: string }): Promise<{ status: string; configuration_revision: number; counts: NeuronImportPreview['counts'] }> {
  const res = await apiFetch(`${API_BASE}/tags/import-neuron`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail?.message || err.detail || `Import Neuron tags failed: ${res.status}`)
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
  includeDisabled = false,
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
  if (includeDisabled) params.set('include_disabled', 'true')
  const res = await apiFetch(`${API_BASE}/tags?${params}`)
  if (!res.ok) throw await authError(res, `Tag list fetch failed: ${res.status}`)
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

export async function updateTag(tagId: string, updates: Partial<Pick<Tag, 'scale_factor' | 'value_offset' | 'unit' | 'display_name'>>): Promise<any> {
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
  raw_value: number | boolean | string | null
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
  total: null
  has_more: boolean
  next_cursor: string | null
  page_size: number
}

export async function fetchTelemetry(
  tagId?: string,
  range: '1h' | '24h' | '7d' | 'all' = '1h',
  cursor: string | null = null,
  pageSize = 50,
  nodeId?: string,
): Promise<TelemetryResponse> {
  const params = new URLSearchParams({ page_size: String(pageSize), range })
  if (cursor) params.set('cursor', cursor)
  if (tagId) params.set('tag_id', tagId)
  if (nodeId) params.set('node_id', nodeId)
  const res = await apiFetch(`${API_BASE}/telemetry?${params}`)
  if (!res.ok) throw await authError(res, `Fetch telemetry failed: ${res.status}`)
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


// ── Node Config Update ──

export interface NodeUpdateRequest {
  name?: string
  node_type?: string
  parent_id?: string | null
  sort_order?: number
  config?: Record<string, any>
}

export async function updateNode(nodeId: string, updates: NodeUpdateRequest): Promise<Node> {
  const res = await apiFetch(`${API_BASE}/nodes/${nodeId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail?.message || err.detail || `Update node failed: ${res.status}`)
  }
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
  configuration_revision: number
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface RuleCreateRequest {
  name: string
  rule_type: 'control' | 'linkage'
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

export interface JdmExecutionSummary {
  id: string
  rule_id: string
  rule_version: number
  frame_id: string
  frame_sequence: number
  configuration_revision: number
  status: 'executed' | 'rejected'
  reason_code: string | null
  outputs: Record<string, any>
  executed_at: string
}

export async function fetchRuleExecutions(
  ruleId: string,
  limit = 1,
): Promise<JdmExecutionSummary[]> {
  const res = await apiFetch(`${API_BASE}/rules/${ruleId}/executions?limit=${limit}`)
  if (!res.ok) throw new Error(`Fetch rule executions failed: ${res.status}`)
  const data = await res.json()
  return data.executions || []
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

// ── Alarms ──

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
  alarm_source?: string | null
  alarm_count?: number | null
  alarm_code?: string | null
  duration_seconds?: number
  state?: 'pending' | 'active_unacknowledged' | 'active_acknowledged' | 'recovered'
}

export interface AlarmListResponse {
  alarms: Alarm[]
  total: number
  page: number
  page_size: number
  total_pages: number
  summary: {
    active: number
    unacknowledged: number
    critical: number
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
  node_name: string
  entity_name: string
  alarm_name: string
  duration_seconds: number
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
      node_name: item.node_name,
      entity_id: item.entity_instance_id,
      entity_name: item.entity_name,
      level: item.severity,
      message: item.alarm_name,
      acknowledged: item.state === 'active_acknowledged',
      ack_user: item.acknowledged_by,
      ack_at: item.acknowledged_at,
      created_at: item.active_at || item.pending_at,
      resolved_at: item.recovered_at,
      duration_seconds: item.duration_seconds,
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
// ── Unified alarm configuration ──

export type AlarmSeverity = 'CRITICAL' | 'MAJOR' | 'WARNING' | 'INFO'
export type AlarmConditionOperator = 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte' | 'contains' | 'not_contains'

export interface AlarmCondition {
  operator: AlarmConditionOperator
  value: AlarmConditionValue
}

// ── L0 -> L1 -> L2 Data Trunk ──

export type DataTrunkQuality = 0 | 1 | 64 | 192
export type PointProcessingPlanAction = 'add' | 'update' | 'preserve' | 'delete_candidate' | 'block'

export interface PointProcessingTemplateInput {
  input_id: string
  source_kind: 'l0' | 'l2'
  source_key: string
  aliases: string[]
  data_type: string
  unit: string | null
  required: boolean
  cardinality: 'one' | 'many'
  selector: {
    scope: 'descendants'
    nodeType: string
    entityDefinition: string
  } | null
  default_value: number | boolean | null
}

export interface PointProcessingTemplate {
  revision_id: string
  asset_id: string
  display_name: string
  device_category: string
  brand: string
  model: string
  revision: number
  status: 'active' | 'retired'
  content_digest: string
  requires_scan: boolean
  inputs: PointProcessingTemplateInput[]
  outputs: Array<{
    output_key: string
    entity_definition_id: string
    data_type: string
    unit: string | null
    freshness_seconds: number
    transform: {
      kind: string
      expression?: string
      canonicalAst?: Record<string, unknown>
      astDigest?: string
      scheduleSeconds?: number
      controlEligible?: boolean
    }
  }>
}

export interface PointProcessingFormulaPreview {
  expression: string
  canonical_ast: Record<string, unknown>
  ast_digest: string
  result_type: string
  result_unit: string | null
  member_count: number
  selector_members: Array<{
    input_id: string
    member_count: number
    member_ids: string[]
    digest: string
  }>
  dag_summary: {
    edge_count: number
    max_depth: number | null
    digest: string | null
  }
  blockers: Array<{ code: string; input_id: string }>
}

export interface NodeDataTrunk {
  node_id: string
  l0: Array<{
    source_id: string
    source_key: string
    data_type: string
    unit: string | null
  }>
  l1_summary: {
    installed: boolean
    revision_id: string | null
    output_count: number
    source_summary: Array<{
      input_id: string
      source_kind: 'l0' | 'l2'
      source_key: string
    }>
    input_bindings?: Record<string, string>
    can_promote?: boolean
  }
  l2: Array<{
    output_key: string
    entity_instance_id: string
    processing_kind: string | null
    source_summary: Array<{
      input_id: string
      source_kind: 'l0' | 'l2'
      source_key: string
    }>
  }>
}

export interface PointProcessingPlanItem {
  item_key: string
  kind: 'l0_point' | 'input_binding' | 'selector_binding' | 'dag_validation' | 'output_binding'
  layer: 'L0' | 'L1' | 'L2'
  action: PointProcessingPlanAction
  input_id?: string
  candidate_source_ids?: string[]
  selected_source_id?: string | null
  selected_source_ids?: string[]
  selector_digest?: string | null
  cardinality?: 'one' | 'many'
  blocker_code?: string | null
  output_id?: string
  entity_definition_id?: string
  planned_edges?: Array<[string, string]>
  max_depth?: number | null
  dag_digest?: string | null
  after?: {
    source_id: string
    name: string
    value_data_type: string
    unit: string | null
    group: string
    source_address: string
  } | null
}

export interface PointProcessingTrialOutput {
  entity_instance_id: string
  entity_definition_id: string
  value: number | boolean | string | string[] | null
  data_type: string
  unit: string | null
  quality: number
  reason: string | null
  observed_at: string
  value_observed_at: string | null
  source_ids: string[]
}

export type PointProcessingTrial = {
  available: true
  frame_sequence: number
  frame_time: string
  configuration_revision: number
  outputs: PointProcessingTrialOutput[]
} | {
  available: false
  reason: string
  message: string
}

export interface PointProcessingPlan {
  id: string
  node_id: string
  template_revision_id: string
  base_configuration_revision: number
  status: 'ready' | 'blocked' | 'applied'
  items: PointProcessingPlanItem[]
  blockers: Array<{ code: string; input_id: string }>
  digest: string
  trial?: PointProcessingTrial | null
}

export interface PointProcessingApplication {
  id: string
  plan_id: string
  installed_processing_id: string
  revision_id: string
  configuration_revision: number
  output_entity_instance_ids: string[]
}

export interface EntityInstanceObservation {
  type?: 'entity_observation'
  event_id: string | null
  entity_instance_id: string
  definition_id: string
  instance_key?: string
  value: number | boolean | string | string[] | null
  data_type: string
  unit: string | null
  quality: DataTrunkQuality
  reason: string | null
  observed_at: string
  received_at?: string | null
  calculated_at?: string | null
  age_ms: number
  fresh?: boolean
  quality_good?: boolean
  processing_revision_id: string | null
  configuration_revision: number | null
  source_digest?: string | null
  source_summary?: { digest?: string } | string | null
}

export class DataTrunkApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string | null,
  ) {
    super(message)
    this.name = 'DataTrunkApiError'
  }

  get retryable(): boolean { return this.status >= 500 }
}

export class DataTrunkResultUnknownError extends Error {
  readonly cause: unknown

  constructor(cause: unknown) {
    super('服务器可能已经完成应用，但响应未能完整读取。请使用同一请求重试。')
    this.name = 'DataTrunkResultUnknownError'
    this.cause = cause
  }
}

async function dataTrunkError(response: Response, fallback: string): Promise<DataTrunkApiError> {
  const payload = await response.json().catch(() => null) as {
    detail?: string | { code?: string; message?: string }
  } | null
  const detail = payload?.detail
  return new DataTrunkApiError(
    typeof detail === 'string' ? detail : detail?.message || fallback,
    response.status,
    typeof detail === 'object' ? detail?.code || null : null,
  )
}

export async function fetchPointProcessingTemplates(deviceCategory: string): Promise<PointProcessingTemplate[]> {
  const response = await apiFetch(`${API_BASE}/point-processing-templates?device_category=${encodeURIComponent(deviceCategory)}`)
  if (!response.ok) throw await dataTrunkError(response, `读取点位加工模板失败：${response.status}`)
  return (await response.json() as { items: PointProcessingTemplate[] }).items
}

export interface PointProcessingTemplateDocumentResult {
  revision_id: string
  content_digest: string
  content: Record<string, unknown>
}

export async function exportPointProcessingTemplate(revisionId: string): Promise<Record<string, unknown>> {
  const response = await apiFetch(`${API_BASE}/point-processing-templates/${encodeURIComponent(revisionId)}/export`)
  if (!response.ok) throw await dataTrunkError(response, `读取模板内容失败：${response.status}`)
  return response.json()
}

export async function validatePointProcessingTemplate(
  content: Record<string, unknown>,
): Promise<PointProcessingTemplateDocumentResult> {
  const response = await apiFetch(`${API_BASE}/point-processing-templates/validate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(content),
  })
  if (!response.ok) throw await dataTrunkError(response, `检查模板失败：${response.status}`)
  return response.json()
}

export async function importPointProcessingTemplate(
  content: Record<string, unknown>,
): Promise<PointProcessingTemplateDocumentResult> {
  const response = await apiFetch(`${API_BASE}/point-processing-templates/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(content),
  })
  if (!response.ok) throw await dataTrunkError(response, `发布模板失败：${response.status}`)
  return response.json()
}

export async function promotePointProcessingTemplate(
  nodeId: string,
  body: { asset_id: string; display_name: string; brand: string; model: string },
): Promise<PointProcessingTemplateDocumentResult> {
  const response = await apiFetch(`${API_BASE}/nodes/${encodeURIComponent(nodeId)}/point-processing-templates/promote`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw await dataTrunkError(response, `保存共享模板失败：${response.status}`)
  return response.json()
}

export async function fetchNodeDataTrunk(nodeId: string): Promise<NodeDataTrunk> {
  const response = await apiFetch(`${API_BASE}/nodes/${encodeURIComponent(nodeId)}/data-trunk`)
  if (!response.ok) throw await dataTrunkError(response, `读取节点数据主干失败：${response.status}`)
  return response.json()
}

export async function createPointProcessingPlan(
  nodeId: string,
  body: { template_revision_id: string; input_selections: Record<string, string> },
): Promise<PointProcessingPlan> {
  const response = await apiFetch(`${API_BASE}/nodes/${encodeURIComponent(nodeId)}/point-processing-plans`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw await dataTrunkError(response, `生成点位加工计划失败：${response.status}`)
  return response.json()
}

export async function createPointProcessingDraftPlan(
  nodeId: string,
  body: { content: Record<string, unknown>; input_selections: Record<string, string> },
): Promise<PointProcessingPlan> {
  const response = await apiFetch(`${API_BASE}/nodes/${encodeURIComponent(nodeId)}/point-processing-drafts/plan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw await dataTrunkError(response, `检查点位加工失败：${response.status}`)
  return response.json()
}

export interface RawPointMaintenanceInput {
  tag_ids: string[]
  display_name?: string
  enabled?: boolean
}

export interface RawPointMaintenanceResult {
  updated: number
  configuration_revision: number
  items: Pick<Tag, 'id' | 'node_id' | 'name' | 'display_name' | 'enabled' | 'source_path'>[]
}

export async function maintainRawPoints(
  input: RawPointMaintenanceInput,
): Promise<RawPointMaintenanceResult> {
  const res = await apiFetch(`${API_BASE}/tags/maintenance`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    const detail = error.detail
    throw new Error(
      typeof detail === 'string'
        ? detail
        : detail?.message || `原始点位维护失败：${res.status}`,
    )
  }
  return res.json()
}

export interface RawPointDeleteResult {
  deleted: number
  configuration_revision: number
  deleted_ids: string[]
}

export async function deleteRawPoints(tagIds: string[]): Promise<RawPointDeleteResult> {
  const res = await apiFetch(`${API_BASE}/tags/maintenance`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tag_ids: tagIds }),
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    const detail = error.detail
    throw new Error(
      typeof detail === 'string'
        ? detail
        : detail?.message || `原始点位删除失败：${res.status}`,
    )
  }
  return res.json()
}

export async function createPointProcessingDeactivationPlan(
  nodeId: string,
): Promise<PointProcessingPlan> {
  const response = await apiFetch(`${API_BASE}/nodes/${encodeURIComponent(nodeId)}/point-processing-deactivation-plan`, {
    method: 'POST',
  })
  if (!response.ok) throw await dataTrunkError(response, `准备停用点位加工失败：${response.status}`)
  return response.json()
}

export async function previewPointProcessingFormula(
  nodeId: string,
  body: { template_revision_id: string; expression: string },
): Promise<PointProcessingFormulaPreview> {
  const response = await apiFetch(`${API_BASE}/nodes/${encodeURIComponent(nodeId)}/point-processing-formula-preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw await dataTrunkError(response, `预检跨节点公式失败：${response.status}`)
  return response.json() as Promise<PointProcessingFormulaPreview>
}

export async function fetchPointProcessingPlan(planId: string): Promise<PointProcessingPlan> {
  const response = await apiFetch(`${API_BASE}/point-processing-plans/${encodeURIComponent(planId)}`)
  if (!response.ok) throw await dataTrunkError(response, `读取点位加工计划失败：${response.status}`)
  return response.json()
}

export async function applyPointProcessingPlan(
  planId: string,
  planDigest: string,
  idempotencyKey: string,
): Promise<PointProcessingApplication> {
  const response = await apiFetch(`${API_BASE}/point-processing-plans/${encodeURIComponent(planId)}/apply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify({ plan_digest: planDigest }),
  })
  if (!response.ok) throw await dataTrunkError(response, `应用点位加工计划失败：${response.status}`)
  try { return await response.json() }
  catch (cause) { throw new DataTrunkResultUnknownError(cause) }
}

export type EntityHistoryRange = '1h' | '6h' | '24h' | '7d'

export async function fetchEntityInstanceHistory(
  entityInstanceId: string,
  range: EntityHistoryRange = '1h',
): Promise<EntityInstanceObservation[]> {
  const response = await apiFetch(`${API_BASE}/entity-instances/${encodeURIComponent(entityInstanceId)}/history?range=${range}`)
  if (!response.ok) throw await dataTrunkError(response, `读取全局实体历史失败：${response.status}`)
  return (await response.json() as { items: EntityInstanceObservation[] }).items
}

export interface AlarmRule {
  id: string
  name: string
  severity: AlarmSeverity
  trigger: AlarmCondition & { value: number | string }
  trigger_duration_seconds: number
  recovery: AlarmCondition & { value: number | string }
  recovery_duration_seconds: number
  notification_throttle_seconds: number
  unit: string | null
  fault_map_id: string | null
}

export interface AlarmRuleSetRevision {
  rule_set_id: string
  key: string
  name: string
  revision: number
  rules: AlarmRule[]
  digest: string
}

export interface AlarmRuleGroup {
  rule_set_id: string
  key: string
  name: string
  latest_revision: number
  last_non_empty_revision: number | null
  entity_instance_ids: string[]
  enabled_entity_instance_ids: string[]
  device_count: number
  rule_count: number
  highest_severity: AlarmSeverity | null
}

export interface AlarmRuleTrial {
  entity_instance_id: string
  trigger_matches: boolean
  recovery_matches: boolean
  description: string
}

export interface AlarmBlocker { code: string; message: string }
export type AlarmPlanAction = 'add' | 'update' | 'preserve' | 'delete_candidate' | 'block'
export type AlarmPlanStatus = 'ready' | 'blocked' | 'applied'

export interface AlarmConfigurationPlanItem {
  definition_key: string
  entity_instance_id: string
  rule_id: string
  action: AlarmPlanAction
  before: unknown
  after: unknown
  blockers: AlarmBlocker[]
}

export interface AlarmConfigurationPlan {
  id: string
  base_configuration_revision: number
  rule_set_revision: AlarmRuleSetRevision
  status: AlarmPlanStatus
  items: AlarmConfigurationPlanItem[]
  blockers: AlarmBlocker[]
  digest: string
}

export interface LegacyAlarmMigrationCandidate {
  source_kind: 'tag_alarm' | 'entity_alarm_binding'
  source_key: string
  display_name: string
  status: 'ready' | 'blocked' | 'migrated'
  severity: AlarmSeverity | null
  entity_instance_id: string | null
  entity_instance_candidates: string[]
  blockers: AlarmBlocker[]
  target_definition_ids: string[]
  proposed_rules: {
    entity_instance_id: string
    display_name: string
    blockers: AlarmBlocker[]
    proposed_definitions: {
      name: string
      severity: AlarmSeverity | null
      trigger: AlarmCondition | null
      trigger_duration_seconds: number
      recovery: AlarmCondition | null
      recovery_duration_seconds: number
      notification_throttle_seconds: number
      blockers: AlarmBlocker[]
    }[]
  }[]
}

export interface LegacyAlarmMigrationPlan {
  installation_id: string
  status: 'ready' | 'blocked' | 'migrated'
  items: LegacyAlarmMigrationCandidate[]
  blockers: AlarmBlocker[]
  digest: string
  target_definition_ids: string[]
}

export interface AlarmConfigurationCurrent {
  configuration_revision: number
  definitions: {
    entity_display_name: string; rule_name: string; severity: AlarmSeverity; trigger: AlarmCondition; recovery: AlarmCondition
    source: string; version_description: string; enabled: boolean; status: 'current'
  }[]
}

export interface AlarmConfigurationApplyResult {
  id: string
  plan_id: string
  configuration_revision: number
  definition_ids: string[]
  audit_event_id: string
  applied_at: string
}

export type AlarmAcceptanceStage = 'waiting_trigger' | 'waiting_acknowledgement' | 'waiting_recovery' | 'passed'

export interface AlarmConfigurationAcceptanceProgressItem {
  definition_id: string
  entity_instance_id: string
  action: 'add' | 'update' | 'preserve'
  rule_name: string
  stage: AlarmAcceptanceStage
  code: string
  event_id: string | null
  event_state: 'pending' | 'active_unacknowledged' | 'active_acknowledged' | 'recovered' | null
  transition_codes: string[]
  acknowledgement_audit_event_id: string | null
}

export interface AlarmConfigurationAcceptanceProgress {
  application_id: string
  site_configuration_version: number
  applied_at: string
  ready_to_report: boolean
  report_id: string | null
  report_status: 'passed' | 'failed' | null
  report_digest: string | null
  items: AlarmConfigurationAcceptanceProgressItem[]
}

export interface AlarmConfigurationAcceptanceReportItem {
  definition_id: string
  definition_key: string
  action: 'add' | 'update' | 'preserve'
  status: 'passed' | 'failed'
  code: string
  event_id: string | null
  event_state: string | null
  transition_codes: string[]
  acknowledgement_audit_event_id: string | null
  evidence: Record<string, unknown>
}

export interface AlarmConfigurationAcceptanceReport {
  id: string
  application_id: string
  installation_id: string
  site_configuration_version: number
  actor: string
  status: 'passed' | 'failed'
  items: AlarmConfigurationAcceptanceReportItem[]
  started_at: string
  finished_at: string
  digest: string
}

export class AlarmConfigurationApiError extends Error {
  constructor(message: string, readonly code: string | null, readonly status: number) {
    super(message)
    this.name = 'AlarmConfigurationApiError'
  }
}

const ALARM_CONFIGURATION_MESSAGES: Record<string, string> = {
  ALARM_PLAN_NOT_FOUND: '未找到该告警配置计划。',
  ALARM_RULE_SET_NOT_FOUND: '未找到所选规则集。',
  ALARM_PLAN_STALE: '配置基线已变化，请重新生成计划。',
  ALARM_PLAN_DIGEST_MISMATCH: '计划摘要已失效，请重新生成计划。',
  ALARM_PLAN_BLOCKED: '计划存在阻断项，不能应用。',
  ALARM_MIGRATION_AMBIGUOUS: '旧配置存在多个实体实例候选，需要明确选择。',
  ALARM_MIGRATION_NOTHING_TO_MIGRATE: '旧配置已全部迁移，没有待生成的迁移计划。',
  ALARM_ENTITY_UNRESOLVED: '存在无法解析的实体实例。',
  ALARM_FAULT_MAP_UNRESOLVED: '存在无法解析的故障映射。',
  ALARM_MIGRATION_SELECTION_INVALID: '所选迁移目标无效，请重新选择。',
  ALARM_MIGRATION_PLAN_STALE: '旧配置候选已变化，请重新检查。',
  ALARM_MIGRATION_PLAN_BLOCKED: '旧配置迁移存在阻断项。',
  ALARM_RULE_CONFLICT: '规则稳定标识冲突。',
  ALARM_BATCH_LIMIT_EXCEEDED: '单次配置范围超过系统上限。',
  ALARM_ACCEPTANCE_APPLICATION_NOT_FOUND: '当前没有可验收的已应用告警配置。',
  ALARM_ACCEPTANCE_APPLICATION_STALE: '该配置已不是最新应用版本，验收证据已刷新。',
  ALARM_ACCEPTANCE_REPORT_NOT_FOUND: '未找到该验收报告。',
  ALARM_ACCEPTANCE_APPLIED_ITEMS_INVALID: '已应用配置的验收范围不完整。',
  ALARM_ACCEPTANCE_PERSISTENCE_UNAVAILABLE: '验收证据暂时不可用，请稍后重试。',
}

async function alarmConfigurationError(response: Response, fallback: string): Promise<AlarmConfigurationApiError> {
  const payload = await response.json().catch(() => null) as {
    detail?: { code?: string; message?: string } | string
  } | null
  const detail = payload?.detail
  const code = typeof detail === 'string' ? null : detail?.code || null
  return new AlarmConfigurationApiError(code ? ALARM_CONFIGURATION_MESSAGES[code] || '告警配置请求未完成，请检查当前配置后重试。' : fallback, code, response.status)
}

async function alarmConfigurationFetch(input: string, init?: RequestInit): Promise<Response> {
  try {
    return await apiFetch(input, init)
  } catch (cause) {
    throw new AlarmConfigurationResultUnknownError(cause)
  }
}

export async function getUnifiedAlarmConfiguration(): Promise<AlarmConfigurationCurrent> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-configurations`)
  if (!response.ok) throw await alarmConfigurationError(response, `读取告警配置失败：${response.status}`)
  return response.json()
}

export async function fetchAlarmRuleSets(): Promise<AlarmRuleSetRevision[]> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-rule-sets`)
  if (!response.ok) throw await alarmConfigurationError(response, `读取规则集失败：${response.status}`)
  return (await response.json() as { items: AlarmRuleSetRevision[] }).items
}

export async function fetchAlarmRuleGroups(): Promise<AlarmRuleGroup[]> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-rule-groups`)
  if (!response.ok) throw await alarmConfigurationError(response, `读取告警规则失败：${response.status}`)
  return (await response.json() as { items: AlarmRuleGroup[] }).items
}

export async function trialAlarmRule(input: {
  entity_instance_id: string
  rule: AlarmRule
  value: AlarmConditionValue | string[]
  quality: number
}): Promise<AlarmRuleTrial> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-configurations/trials`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  })
  if (!response.ok) throw await alarmConfigurationError(response, `试算失败：${response.status}`)
  return response.json()
}

export async function createAlarmRuleSet(input: Pick<AlarmRuleSetRevision, 'key' | 'name' | 'rules'>): Promise<AlarmRuleSetRevision> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-rule-sets`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  })
  if (!response.ok) throw await alarmConfigurationError(response, `创建规则集失败：${response.status}`)
  return response.json()
}

export async function createAlarmRuleSetRevision(ruleSetId: string, rules: AlarmRule[]): Promise<AlarmRuleSetRevision> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-rule-sets/${encodeURIComponent(ruleSetId)}/revisions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rules }),
  })
  if (!response.ok) throw await alarmConfigurationError(response, `保存规则修订失败：${response.status}`)
  return response.json()
}

export async function createAlarmConfigurationPlan(input: {
  selection: { entity_instance_ids: string[]; node_ids: string[]; entity_definition_ids: string[] }
  rule_set_id: string
  rule_set_revision: number
}): Promise<AlarmConfigurationPlan> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-configuration-plans`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  })
  if (!response.ok) throw await alarmConfigurationError(response, `生成配置计划失败：${response.status}`)
  return response.json()
}

export async function getAlarmConfigurationPlan(planId: string): Promise<AlarmConfigurationPlan> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-configuration-plans/${encodeURIComponent(planId)}`)
  if (!response.ok) throw await alarmConfigurationError(response, `读取配置计划失败：${response.status}`)
  return response.json()
}

export async function applyAlarmConfigurationPlan(planId: string, planDigest: string, idempotencyKey: string): Promise<AlarmConfigurationApplyResult> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-configuration-plans/${encodeURIComponent(planId)}/apply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify({ plan_digest: planDigest }),
  })
  if (!response.ok) throw await alarmConfigurationError(response, `应用配置计划失败：${response.status}`)
  return readAlarmConfigurationApplyResult<AlarmConfigurationApplyResult>(response)
}

export async function fetchAlarmConfigurationAcceptanceProgress(): Promise<AlarmConfigurationAcceptanceProgress> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-configuration-applications/latest/acceptance-progress`)
  if (!response.ok) throw await alarmConfigurationError(response, `读取验收进度失败：${response.status}`)
  return response.json()
}

export async function runAlarmConfigurationAcceptance(applicationId: string, idempotencyKey: string): Promise<AlarmConfigurationAcceptanceReport> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-configuration-applications/${encodeURIComponent(applicationId)}/acceptance`, {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
  })
  if (!response.ok) throw await alarmConfigurationError(response, `生成验收报告失败：${response.status}`)
  try {
    return await response.json()
  } catch (cause) {
    throw new AlarmConfigurationResultUnknownError(cause)
  }
}

export async function fetchAlarmConfigurationReport(reportId: string): Promise<AlarmConfigurationAcceptanceReport> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-configuration-reports/${encodeURIComponent(reportId)}`)
  if (!response.ok) throw await alarmConfigurationError(response, `读取验收报告失败：${response.status}`)
  return response.json()
}

export async function fetchLegacyAlarmMigrationCandidates(): Promise<{ installation_id: string; items: LegacyAlarmMigrationCandidate[] }> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-configuration-migrations/legacy`)
  if (!response.ok) throw await alarmConfigurationError(response, `读取旧配置候选失败：${response.status}`)
  return response.json()
}

export async function createLegacyAlarmMigrationPlan(input: {
  installation_id: string
  selections: { source_kind: LegacyAlarmMigrationCandidate['source_kind']; source_key: string; entity_instance_id: string }[]
}): Promise<AlarmConfigurationPlan> {
  const response = await alarmConfigurationFetch(`${API_BASE}/alarm-configuration-migrations/legacy/plans`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  })
  if (!response.ok) throw await alarmConfigurationError(response, `生成旧配置迁移计划失败：${response.status}`)
  return response.json()
}

// ── Global Entities ──

export interface EntityInstance {
  id: string
  node_id: string
  node_type: string
  node_display_name: string
  definition_id: string
  display_name: string
  data_type: string
  unit: string | null
  direction: 'R' | 'W' | 'RW'
  freshness_seconds: number
  confirmed: boolean
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
  node_id: string
  node_name: string
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
  configuration_revision: number
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

export async function fetchEntityInstances(): Promise<{ items: EntityInstance[]; total: number }> {
  const res = await apiFetch(`${API_BASE}/entity-instances`)
  if (!res.ok) throw new Error(`Fetch entity instances failed: ${res.status}`)
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
