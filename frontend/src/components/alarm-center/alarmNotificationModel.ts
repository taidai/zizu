export type AlarmNotificationDeliveryStatus =
  | 'pending'
  | 'retry_wait'
  | 'delivered'
  | 'failed'
  | 'cancelled'

const STATUS_LABELS: Record<string, string> = {
  pending: '待发送',
  retry_wait: '等待重试',
  delivered: '已送达',
  failed: '发送失败',
  cancelled: '已取消',
}

const EVENT_LABELS: Record<string, string> = {
  ALARM_ACTIVATED: '告警发生',
  ALARM_RECOVERED: '告警恢复',
}

const ERROR_LABELS: Record<string, string> = {
  HTTP_NOTIFICATION_DELIVERY_REJECTED: '目标服务拒绝请求',
  HTTP_NOTIFICATION_DELIVERY_TIMEOUT: '目标服务响应超时',
  HTTP_NOTIFICATION_DELIVERY_NETWORK_ERROR: '目标服务无法连接',
  HTTP_NOTIFICATION_INVALID_TEMPLATE: '请求模板无法生成',
  HTTP_NOTIFICATION_DELIVERY_CANCELLED: '通知配置已删除',
  LEGACY_NOTIFICATION_NOT_REPLAYED: '旧通知未重放',
}

export function describeDeliveryStatus(status: string): string {
  return STATUS_LABELS[status] || status || '未知'
}

export function describeDeliveryEvent(eventType: string | null): string {
  return eventType ? EVENT_LABELS[eventType] || eventType : '历史通知'
}

export function describeDeliveryError(code: string | null): string {
  return code ? ERROR_LABELS[code] || code : '—'
}

export function canRetryDelivery(delivery: {
  status: string
  configuration_exists: boolean
}): boolean {
  return delivery.status === 'failed' && delivery.configuration_exists
}
