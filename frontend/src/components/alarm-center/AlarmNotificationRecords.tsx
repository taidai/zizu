import { useEffect, useState } from 'react'
import {
  deleteAlarmNotificationDeliveries,
  fetchAlarmNotificationDeliveries,
  retryAlarmNotificationDelivery,
  type AlarmNotificationDelivery,
} from '../../api/client'
import {
  canDeleteDelivery,
  canRetryDelivery,
  deletableDeliveryIds,
  describeDeliveryError,
  describeDeliveryEvent,
  describeDeliveryStatus,
  validDeliveryPage,
} from './alarmNotificationModel'

const STATUS_STYLE: Record<string, string> = {
  pending: 'bg-blue-50 text-blue-700',
  retry_wait: 'bg-amber-50 text-amber-700',
  delivered: 'bg-green-50 text-green-700',
  failed: 'bg-red-50 text-red-700',
  cancelled: 'bg-gray-100 text-gray-500',
}

function localTime(value: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}

export default function AlarmNotificationRecords({ canManage }: { canManage: boolean }) {
  const [items, setItems] = useState<AlarmNotificationDelivery[]>([])
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<10 | 20>(10)
  const [totalPages, setTotalPages] = useState(1)
  const [detail, setDetail] = useState<AlarmNotificationDelivery | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const load = async (targetPage = page) => {
    setBusy('load')
    setError('')
    try {
      const result = await fetchAlarmNotificationDeliveries(targetPage, pageSize)
      const validPage = validDeliveryPage(targetPage, result.total_pages)
      if (validPage !== targetPage) {
        setPage(validPage)
        return
      }
      setItems(result.items)
      setTotalPages(result.total_pages)
      setSelected(new Set())
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取通知记录。')
    } finally {
      setBusy('')
    }
  }

  useEffect(() => { void load(page) }, [page, pageSize])

  const retry = async (delivery: AlarmNotificationDelivery) => {
    setBusy(`retry:${delivery.id}`)
    setError('')
    setMessage('')
    try {
      await retryAlarmNotificationDelivery(delivery.id, crypto.randomUUID())
      setMessage('已重新加入发送队列。')
      await load(page)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '重新发送失败。')
    } finally {
      setBusy('')
    }
  }

  const remove = async (deliveryIds: string[]) => {
    if (!deliveryIds.length) return
    const label = deliveryIds.length === 1 ? '这条通知记录' : `选中的 ${deliveryIds.length} 条通知记录`
    if (!window.confirm(`确定永久删除${label}及其发送详情吗？删除后无法恢复。`)) return
    setBusy('delete')
    setError('')
    setMessage('')
    try {
      const result = await deleteAlarmNotificationDeliveries(deliveryIds)
      setMessage(`已永久删除 ${result.deleted} 条通知记录。`)
      await load(page)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除通知记录失败。')
    } finally {
      setBusy('')
    }
  }

  const deletableIds = deletableDeliveryIds(items)
  const allSelected = deletableIds.length > 0 && deletableIds.every((id) => selected.has(id))

  const toggle = (id: string, checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-bold text-gray-800">通知记录</h2>
          <p className="mt-1 text-xs text-gray-500">查看每次告警发生或恢复后的 HTTP 发送结果；通知失败不会改变告警状态。</p>
          <p className="mt-1 text-xs text-gray-500">没有新记录？先核对规则的生效条件与 HTTP 绑定。同一次未恢复告警不会重复发送；启停、确认不会补发消息。</p>
        </div>
        <div className="flex gap-2">
          {canManage && selected.size > 0 && (
            <button type="button" disabled={busy !== ''} onClick={() => void remove([...selected])} className="neu-btn px-3 py-2 text-xs text-red-600 disabled:opacity-40">
              删除所选（{selected.size}）
            </button>
          )}
          <button type="button" disabled={busy !== ''} onClick={() => void load(page)} className="neu-btn px-3 py-2 text-xs text-gray-700 disabled:opacity-40">
            刷新
          </button>
          <label className="flex items-center text-xs text-gray-600">每页
            <select aria-label="通知每页条数" value={pageSize} onChange={(event) => { setPage(1); setSelected(new Set()); setPageSize(Number(event.target.value) as 10 | 20) }} className="neu-input mx-2 px-2 py-1.5">
              <option value={10}>10</option><option value={20}>20</option>
            </select>条
          </label>
        </div>
      </div>

      {!canManage && <p className="rounded-lg border border-white/70 bg-white/40 px-3 py-2 text-xs text-gray-600">当前账户仅可查看通知投递与尝试详情；重新发送和删除需要实施工程师或管理员权限。</p>}

      {error && <p role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</p>}
      {message && <p className="rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-700">{message}</p>}

      <div className="neu-card alarm-table-viewport">
        <table className="alarm-notification-table w-full table-fixed text-left text-xs" data-testid="alarm-notification-table">
          <thead className="border-b border-gray-200 text-gray-500">
            <tr>
              <th className="w-10 p-3">
                <input
                  type="checkbox"
                  aria-label="全选当前页可删除记录"
                  checked={allSelected}
                  disabled={!canManage || deletableIds.length === 0 || busy !== ''}
                  onChange={(event) => setSelected(event.target.checked ? new Set(deletableIds) : new Set())}
                />
              </th>
              <th className="p-3">告警</th><th className="p-3">通知与目标</th><th className="p-3">发送结果</th><th className="p-3">创建时间</th><th className="p-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
          {items.map((delivery) => (
            <tr key={delivery.id} className="border-b border-gray-100 align-top">
              <td className="p-3">
                <input
                  type="checkbox"
                  aria-label={`选择 ${delivery.alarm_name || '告警通知'}`}
                  checked={selected.has(delivery.id)}
                  disabled={!canManage || !canDeleteDelivery(delivery) || busy !== ''}
                  title={canDeleteDelivery(delivery) ? '选择后可永久删除' : '发送中的记录不能删除'}
                  onChange={(event) => toggle(delivery.id, event.target.checked)}
                />
              </td>
              <td className="p-3">
                <article><div className="flex flex-wrap items-center gap-2">
                  <strong className="text-sm text-gray-800">{delivery.alarm_name || '告警通知'}</strong>
                  <span className="rounded bg-orange-50 px-2 py-0.5 text-[10px] text-orange-700">{delivery.severity || '—'}</span>
                  <span className="rounded bg-indigo-50 px-2 py-0.5 text-[10px] text-indigo-700">{describeDeliveryEvent(delivery.event_type)}</span>
                </div>
                <p className="mt-1 truncate text-gray-500" title={`${delivery.node_name || '未知节点'} / ${delivery.entity_name || '未知实体'}`}>{delivery.node_name || '未知节点'} / {delivery.entity_name || '未知实体'}</p><span className="sr-only">{describeDeliveryStatus(delivery.status)}</span></article>
              </td>
              <td className="p-3">
                <p className="text-[10px] text-gray-400">通知与目标</p>
                <p className="mt-1 text-gray-700">{delivery.configuration_name || '配置已删除'}</p>
                <p className="mt-0.5 truncate font-mono text-[10px] text-gray-400" title={delivery.target_display || ''}>{delivery.target_display || '—'}</p>
              </td>
              <td className="p-3">
                <p className="text-[10px] text-gray-400">发送结果</p>
                <span className={`mt-1 inline-block rounded px-2 py-1 text-[10px] ${STATUS_STYLE[delivery.status] || 'bg-gray-100 text-gray-600'}`}>
                  {describeDeliveryStatus(delivery.status)}
                </span>
                <span className="ml-2 text-gray-500">共 {delivery.attempt_count} 次{delivery.last_http_status ? ` · HTTP ${delivery.last_http_status}` : ''}</span>
              </td>
              <td className="p-3">
                <p className="text-[10px] text-gray-400">创建时间</p>
                <p className="mt-1 text-gray-600">{localTime(delivery.created_at)}</p>
                {delivery.last_error_code && <p className="mt-1 text-[10px] text-red-600">{describeDeliveryError(delivery.last_error_code)}</p>}
              </td>
              <td className="p-3">
                <div className="flex justify-end gap-2">
                <button type="button" onClick={() => setDetail(delivery)} className="neu-btn px-3 py-1.5 text-xs text-gray-600">详情</button>
                {canManage && canRetryDelivery(delivery) && (
                  <button type="button" disabled={busy !== ''} onClick={() => void retry(delivery)} className="neu-btn px-3 py-1.5 text-xs text-blue-600 disabled:opacity-40">
                    重新发送
                  </button>
                )}
                {canManage && canDeleteDelivery(delivery) && (
                  <button type="button" disabled={busy !== ''} onClick={() => void remove([delivery.id])} className="neu-btn px-3 py-1.5 text-xs text-red-600 disabled:opacity-40">
                    删除
                  </button>
                )}
                </div>
              </td>
            </tr>
          ))}
          {!items.length && busy !== 'load' && <tr><td colSpan={6} className="p-8 text-center text-sm text-gray-400">暂无通知记录</td></tr>}
          {busy === 'load' && <tr><td colSpan={6} className="p-4 text-center text-xs text-gray-400">正在读取通知记录...</td></tr>}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-3 text-xs text-gray-500">
          <button type="button" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))} className="neu-btn px-3 py-1.5 disabled:opacity-30">上一页</button>
          <span>{page} / {totalPages}</span>
          <button type="button" disabled={page >= totalPages} onClick={() => setPage((value) => Math.min(totalPages, value + 1))} className="neu-btn px-3 py-1.5 disabled:opacity-30">下一页</button>
        </div>
      )}

      {detail && <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/35 p-4" role="dialog" aria-modal="true" aria-label="HTTP 投递详情">
        <div className="neu-card tablet-alarm-dialog max-h-[90vh] w-full max-w-4xl overflow-y-auto p-5">
          <div className="flex items-start justify-between gap-3"><div><h3 className="text-base font-bold text-gray-800">HTTP 投递详情</h3><p className="mt-1 text-xs text-gray-500">{detail.alarm_name || '告警通知'} · {describeDeliveryEvent(detail.event_type)} · {describeDeliveryStatus(detail.status)}</p></div><button type="button" onClick={() => setDetail(null)} className="neu-btn px-3 py-2 text-xs">关闭</button></div>
          <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-3"><div><dt className="text-gray-400">通知配置</dt><dd className="mt-1">{detail.configuration_name || '配置已删除'}</dd></div><div><dt className="text-gray-400">创建 / 送达</dt><dd className="mt-1">{localTime(detail.created_at)}<br />{localTime(detail.delivered_at)}</dd></div><div><dt className="text-gray-400">最后结果</dt><dd className="mt-1">{detail.last_http_status ? `HTTP ${detail.last_http_status}` : describeDeliveryError(detail.last_error_code)}</dd></div></dl>
          {(detail.last_error_detail || detail.last_response_excerpt) && <div className="mt-4 grid gap-2 text-xs sm:grid-cols-2"><p className="neu-inset p-3">最后错误：{detail.last_error_detail || '—'}</p><p className="neu-inset p-3">最后响应：{detail.last_response_excerpt || '—'}</p></div>}
          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[700px] text-left text-[11px]">
              <thead className="text-gray-400"><tr><th className="py-2">次数</th><th>时间</th><th>请求</th><th>结果</th><th>HTTP</th><th>耗时</th><th>说明</th></tr></thead>
              <tbody>{detail.attempts.map((attempt) => <tr key={attempt.attempt_no} className="border-t border-gray-100 text-gray-600"><td className="py-2">{attempt.attempt_no}</td><td>{localTime(attempt.attempted_at)}</td><td className="font-mono">{attempt.method} {attempt.target_display}</td><td>{attempt.outcome}</td><td>{attempt.http_status ? `HTTP ${attempt.http_status}` : '—'}</td><td>{attempt.duration_ms} ms</td><td>{attempt.error_detail || attempt.response_excerpt || '—'}</td></tr>)}</tbody>
            </table>
            {detail.attempts.length === 0 && <p className="py-5 text-center text-xs text-gray-400">尚未尝试发送。</p>}
          </div>
        </div>
      </div>}
    </div>
  )
}
