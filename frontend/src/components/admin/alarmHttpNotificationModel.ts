export interface HttpNotificationFieldLike {
  key: string
  value?: string
  sensitive: boolean
  configured?: boolean
  clear?: boolean
}

export interface HttpNotificationEditable {
  name: string
  description: string | null
  method: string
  url?: string
  url_display?: string
  query_params: HttpNotificationFieldLike[]
  headers: HttpNotificationFieldLike[]
  content_type: string
  body_template: string
  timeout_seconds: number
  current_digest?: string
  tested_digest?: string | null
  tested_at?: string | null
  last_test_status?: unknown | null
  enabled?: boolean
}

export const HTTP_NOTIFICATION_VARIABLES = [
  ['event.type', '发生或恢复'],
  ['event.time', '事件时间'],
  ['alarm.name', '告警名称'],
  ['alarm.severity', '告警等级'],
  ['alarm.state', '告警状态'],
  ['node.name', '节点名称'],
  ['node.path', '节点路径'],
  ['entity.name', '实体名称'],
  ['entity.value', '实体值'],
  ['entity.value_text', '实体值（文本）'],
  ['entity.unit', '实体单位'],
  ['entity.quality', '实体质量'],
  ['entity.observed_at', '实体数据时间'],
] as const

const MATERIAL_FIELDS = [
  'method',
  'url',
  'url_display',
  'query_params',
  'headers',
  'content_type',
  'body_template',
  'timeout_seconds',
] as const

function materialValue(value: HttpNotificationEditable): string {
  return JSON.stringify(MATERIAL_FIELDS.map((field) => value[field]))
}

export function applyHttpNotificationEdit<T extends HttpNotificationEditable>(
  current: T,
  patch: Partial<T>,
): T {
  const next = { ...current, ...patch }
  if (materialValue(current) !== materialValue(next)) {
    return {
      ...next,
      enabled: false,
      tested_digest: null,
      tested_at: null,
      last_test_status: null,
    }
  }
  return next
}

function maskedTarget(source: string): string {
  if (!source.trim()) return '（地址未填写）'
  try {
    const url = new URL(source)
    const query = new URLSearchParams()
    url.searchParams.forEach((_value, key) => query.append(key, '***'))
    const suffix = query.size ? `?${query.toString()}` : ''
    return `${url.protocol}//${url.host}/***${suffix}`
  } catch {
    return source.includes('***') ? source : '（地址格式不正确）'
  }
}

function previewFields(fields: HttpNotificationFieldLike[]): Record<string, string> {
  return Object.fromEntries(
    fields
      .filter((field) => !field.clear && field.key.trim())
      .map((field) => [
        field.key.trim(),
        field.sensitive ? '***' : field.value || '',
      ]),
  )
}

export function buildMaskedPreview(value: HttpNotificationEditable): string {
  return JSON.stringify(
    {
      method: value.method,
      url: maskedTarget(value.url?.trim() || value.url_display || ''),
      query_params: previewFields(value.query_params),
      headers: previewFields(value.headers),
      content_type: value.content_type,
      body: value.body_template,
      timeout_seconds: value.timeout_seconds,
    },
    null,
    2,
  )
}

const ERROR_MESSAGES: Record<string, string> = {
  HTTP_NOTIFICATION_INVALID_TEMPLATE: '请求配置不完整，请检查地址、参数、请求头和正文模板。',
  HTTP_NOTIFICATION_UNSUPPORTED_TARGET: '该地址不允许访问，请使用现场允许的 HTTP 或 HTTPS 地址。',
  HTTP_NOTIFICATION_SECRET_KEY_NOT_CONFIGURED: '服务器尚未配置通知加密密钥，请联系管理员。',
  HTTP_NOTIFICATION_NOT_FOUND: '通知配置不存在或已被删除。',
  HTTP_NOTIFICATION_NOT_TESTED: '请先发送测试，成功后再启用。',
  HTTP_NOTIFICATION_TEST_STALE: '请求内容已修改，请重新发送测试，成功后再启用。',
  HTTP_NOTIFICATION_PERSISTENCE_UNAVAILABLE: '通知配置暂时无法保存，请稍后重试。',
  HTTP_NOTIFICATION_DELIVERY_REJECTED: '目标服务拒绝了请求，请检查地址和请求内容。',
  HTTP_NOTIFICATION_DELIVERY_TIMEOUT: '目标服务响应超时，请检查网络或适当增大超时秒数。',
  HTTP_NOTIFICATION_DELIVERY_NETWORK_ERROR: '无法连接目标服务，请检查网络和地址。',
  HTTP_NOTIFICATION_DELIVERY_NOT_TERMINAL: '发送中的通知记录不能删除，请等待发送结束。',
  HTTP_NOTIFICATION_DELIVERY_NOT_FOUND: '通知记录不存在或已被删除。',
  HTTP_NOTIFICATION_DELIVERY_SELECTION_INVALID: '请选择 1 至 200 条互不重复的通知记录。',
}

export function describeHttpNotificationError(code: string | null | undefined): string {
  if (!code) return 'HTTP 通知请求未完成，请检查配置后重试。'
  return ERROR_MESSAGES[code] || 'HTTP 通知请求未完成，请检查配置后重试。'
}
