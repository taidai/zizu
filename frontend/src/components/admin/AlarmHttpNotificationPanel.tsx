import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  createAlarmHttpNotification,
  deleteAlarmHttpNotification,
  disableAlarmHttpNotification,
  enableAlarmHttpNotification,
  fetchAlarmHttpNotifications,
  testAlarmHttpNotification,
  updateAlarmHttpNotification,
  type AlarmHttpNotificationConfig,
  type AlarmHttpNotificationRequest,
  type HttpNotificationField,
} from '../../api/client'
import {
  HTTP_NOTIFICATION_VARIABLES,
  buildMaskedPreview,
  describeHttpNotificationError,
} from './alarmHttpNotificationModel'

type FieldGroup = 'query_params' | 'headers'

const EMPTY_DRAFT: AlarmHttpNotificationRequest = {
  name: '',
  description: null,
  method: 'POST',
  url: '',
  query_params: [],
  headers: [],
  content_type: 'application/json',
  body_template: `{
  "event": {{event.type}},
  "time": {{event.time}},
  "alarm": {{alarm.name}},
  "severity": {{alarm.severity}},
  "node": {{node.name}},
  "entity": {{entity.name}},
  "value": {{entity.value}}
}`,
  timeout_seconds: 5,
}

function errorMessage(reason: unknown): string {
  if (reason && typeof reason === 'object' && 'code' in reason) {
    return describeHttpNotificationError(String(reason.code || ''))
  }
  return reason instanceof Error ? reason.message : 'HTTP 通知操作失败，请稍后重试。'
}

function configDraft(config: AlarmHttpNotificationConfig): AlarmHttpNotificationRequest {
  return {
    name: config.name,
    description: config.description,
    method: config.method,
    url: '',
    query_params: config.query_params.map((field) => ({ ...field, value: field.value || '' })),
    headers: config.headers.map((field) => ({ ...field, value: field.value || '' })),
    content_type: config.content_type,
    body_template: config.body_template,
    timeout_seconds: config.timeout_seconds,
  }
}

function FieldEditor({
  title,
  fields,
  onChange,
}: {
  title: string
  fields: HttpNotificationField[]
  onChange: (fields: HttpNotificationField[]) => void
}) {
  const patchField = (index: number, patch: Partial<HttpNotificationField>) => {
    onChange(fields.map((field, itemIndex) => itemIndex === index ? { ...field, ...patch } : field))
  }
  const removeField = (index: number) => {
    const field = fields[index]
    if (field.sensitive && field.configured) {
      patchField(index, { clear: !field.clear, value: '' })
      return
    }
    onChange(fields.filter((_, itemIndex) => itemIndex !== index))
  }
  return (
    <fieldset className="rounded-lg border border-gray-200 p-3">
      <div className="flex items-center justify-between">
        <legend className="text-xs font-semibold text-gray-700">{title}</legend>
        <button
          type="button"
          onClick={() => onChange([...fields, { key: '', value: '', sensitive: false }])}
          className="neu-btn px-2 py-1 text-[11px] text-blue-600"
        >
          添加
        </button>
      </div>
      <div className="mt-2 space-y-2">
        {fields.map((field, index) => (
          <div key={index} className={`grid gap-2 sm:grid-cols-[1fr_1.4fr_auto_auto] ${field.clear ? 'opacity-50' : ''}`}>
            <input
              aria-label={`${title} ${index + 1} 名称`}
              value={field.key}
              disabled={field.clear}
              onChange={(event) => patchField(index, { key: event.target.value })}
              placeholder="名称"
              className="neu-input px-2 py-1.5 text-xs"
            />
            <input
              aria-label={`${title} ${index + 1} 值`}
              value={field.value || ''}
              disabled={field.clear}
              type={field.sensitive ? 'password' : 'text'}
              onChange={(event) => patchField(index, { value: event.target.value })}
              placeholder={field.sensitive && field.configured ? '留空保持原值' : '值'}
              className="neu-input px-2 py-1.5 text-xs"
            />
            <label className="flex items-center gap-1 text-[11px] text-gray-600">
              <input
                type="checkbox"
                checked={field.sensitive}
                disabled={field.configured || field.clear}
                onChange={(event) => patchField(index, { sensitive: event.target.checked })}
              />
              敏感
            </label>
            <button
              type="button"
              onClick={() => removeField(index)}
              className={`text-[11px] ${field.clear ? 'text-blue-600' : 'text-red-500'}`}
            >
              {field.clear ? '恢复' : field.sensitive && field.configured ? '清除' : '移除'}
            </button>
          </div>
        ))}
        {fields.length === 0 && <p className="text-[11px] text-gray-400">无</p>}
      </div>
    </fieldset>
  )
}

export default function AlarmHttpNotificationPanel() {
  const [items, setItems] = useState<AlarmHttpNotificationConfig[]>([])
  const [editingId, setEditingId] = useState<string | null | undefined>(undefined)
  const [draft, setDraft] = useState<AlarmHttpNotificationRequest>(EMPTY_DRAFT)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const bodyEditor = useRef<HTMLTextAreaElement>(null)
  const pendingCaret = useRef<number | null>(null)

  useLayoutEffect(() => {
    if (pendingCaret.current === null || !bodyEditor.current) return
    bodyEditor.current.focus()
    bodyEditor.current.setSelectionRange(pendingCaret.current, pendingCaret.current)
    pendingCaret.current = null
  })

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      setItems(await fetchAlarmHttpNotifications())
    } catch (reason) {
      setError(errorMessage(reason))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const selected = useMemo(
    () => editingId ? items.find((item) => item.id === editingId) || null : null,
    [editingId, items],
  )

  const startCreate = () => {
    setEditingId(null)
    setDraft({ ...EMPTY_DRAFT, query_params: [], headers: [] })
    setMessage('')
    setError('')
  }

  const startEdit = (config: AlarmHttpNotificationConfig) => {
    setEditingId(config.id)
    setDraft(configDraft(config))
    setMessage('')
    setError('')
  }

  const patchFields = (group: FieldGroup, fields: HttpNotificationField[]) => {
    setDraft((current) => ({ ...current, [group]: fields }))
  }

  const save = async () => {
    if (!draft.name.trim() || (!editingId && !draft.url.trim())) {
      setError('请填写名称和请求地址。')
      return
    }
    setBusy('save')
    setError('')
    setMessage('')
    try {
      const saved = editingId
        ? await updateAlarmHttpNotification(editingId, draft)
        : await createAlarmHttpNotification(draft)
      await load()
      setEditingId(saved.id)
      setDraft(configDraft(saved))
      setMessage('已保存。请求内容变化后，需要重新发送测试。')
    } catch (reason) {
      setError(errorMessage(reason))
    } finally {
      setBusy('')
    }
  }

  const action = async (config: AlarmHttpNotificationConfig, kind: 'test' | 'enable' | 'disable') => {
    setBusy(`${kind}:${config.id}`)
    setError('')
    setMessage('')
    try {
      const updated = kind === 'test'
        ? await testAlarmHttpNotification(config.id)
        : kind === 'enable'
          ? await enableAlarmHttpNotification(config.id)
          : await disableAlarmHttpNotification(config.id)
      setItems((current) => current.map((item) => item.id === updated.id ? updated : item))
      if (editingId === updated.id) setDraft(configDraft(updated))
      setMessage(kind === 'test' ? '测试请求已送达，可以启用。' : kind === 'enable' ? '通知已启用。' : '通知已停用。')
    } catch (reason) {
      setError(errorMessage(reason))
    } finally {
      setBusy('')
    }
  }

  const remove = async (config: AlarmHttpNotificationConfig) => {
    if (!window.confirm(`确定删除“${config.name}”吗？\n这会解除告警规则绑定，并取消尚未完成的通知。`)) return
    setBusy(`delete:${config.id}`)
    setError('')
    try {
      await deleteAlarmHttpNotification(config.id)
      if (editingId === config.id) setEditingId(undefined)
      await load()
      setMessage('通知配置已删除。')
    } catch (reason) {
      setError(errorMessage(reason))
    } finally {
      setBusy('')
    }
  }

  const insertVariable = (name: string) => {
    const editor = bodyEditor.current
    if (!editor) return
    const variable = `{{${name}}}`
    const { selectionStart, selectionEnd } = editor
    pendingCaret.current = selectionStart + variable.length
    setDraft((current) => ({
      ...current,
      body_template: current.body_template.slice(0, selectionStart) + variable + current.body_template.slice(selectionEnd),
    }))
  }

  return (
    <section className="neu-card p-4" aria-label="HTTP 通知">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-bold text-gray-800">HTTP 通知</h3>
          <p className="mt-1 text-xs text-gray-500">告警发生或恢复后，向指定地址发送 HTTP 请求。</p>
        </div>
        <button type="button" onClick={startCreate} className="neu-btn bg-[#52c41a] px-3 py-1.5 text-xs font-medium text-white">
          新增通知
        </button>
      </header>

      {message && <p className="mt-3 rounded bg-green-50 px-3 py-2 text-xs text-green-700">{message}</p>}
      {error && <p className="mt-3 rounded bg-red-50 px-3 py-2 text-xs text-red-600">{error}</p>}

      <div className="mt-4 space-y-2">
        {items.map((config) => {
          const tested = config.tested_digest === config.current_digest && config.last_test_status?.delivered
          return (
            <article key={config.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-gray-200 bg-white/40 p-3">
              <div className="min-w-[220px]">
                <div className="flex items-center gap-2">
                  <strong className="text-sm text-gray-800">{config.name}</strong>
                  <span className={`rounded px-2 py-0.5 text-[10px] ${config.enabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'}`}>
                    {config.enabled ? '已启用' : '已停用'}
                  </span>
                  <span className={`rounded px-2 py-0.5 text-[10px] ${tested ? 'bg-blue-50 text-blue-600' : 'bg-amber-50 text-amber-700'}`}>
                    {tested ? '测试有效' : '需要测试'}
                  </span>
                </div>
                <p className="mt-1 font-mono text-[11px] text-gray-500">{config.method} {config.url_display}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <button type="button" onClick={() => startEdit(config)} className="neu-btn px-3 py-1.5 text-xs text-gray-700">编辑</button>
                <button type="button" disabled={busy !== ''} onClick={() => void action(config, 'test')} className="neu-btn px-3 py-1.5 text-xs text-blue-600">发送测试</button>
                <button
                  type="button"
                  disabled={busy !== '' || (!config.enabled && !tested)}
                  onClick={() => void action(config, config.enabled ? 'disable' : 'enable')}
                  className="neu-btn px-3 py-1.5 text-xs text-[#389e0d] disabled:opacity-40"
                >
                  {config.enabled ? '停用' : '启用'}
                </button>
                <button type="button" disabled={busy !== ''} onClick={() => void remove(config)} className="neu-btn px-3 py-1.5 text-xs text-red-500">删除</button>
              </div>
            </article>
          )
        })}
        {!loading && items.length === 0 && <p className="rounded-lg border border-dashed border-gray-300 p-5 text-center text-xs text-gray-400">尚未配置 HTTP 通知。</p>}
        {loading && <p className="py-4 text-center text-xs text-gray-400">正在读取通知配置...</p>}
      </div>

      {editingId !== undefined && (
        <div className="mt-4 rounded-xl border border-blue-100 bg-blue-50/30 p-4">
          <div className="flex items-center justify-between">
            <h4 className="text-sm font-semibold text-gray-800">{editingId ? `编辑：${selected?.name || ''}` : '新增 HTTP 通知'}</h4>
            <button type="button" onClick={() => setEditingId(undefined)} className="text-xs text-gray-500">收起</button>
          </div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <label className="text-xs text-gray-600">名称
              <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} className="neu-input mt-1 w-full px-3 py-2" placeholder="例如：值班群" />
            </label>
            <label className="text-xs text-gray-600">说明
              <input value={draft.description || ''} onChange={(event) => setDraft({ ...draft, description: event.target.value || null })} className="neu-input mt-1 w-full px-3 py-2" placeholder="可选" />
            </label>
            <label className="text-xs text-gray-600">HTTP 方法
              <select value={draft.method} onChange={(event) => setDraft({ ...draft, method: event.target.value as AlarmHttpNotificationRequest['method'] })} className="neu-input mt-1 w-full px-3 py-2">
                {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((method) => <option key={method}>{method}</option>)}
              </select>
            </label>
            <label className="text-xs text-gray-600">超时秒数
              <input type="number" min={1} max={30} value={draft.timeout_seconds} onChange={(event) => setDraft({ ...draft, timeout_seconds: Number(event.target.value) })} className="neu-input mt-1 w-full px-3 py-2" />
            </label>
            <label className="text-xs text-gray-600 md:col-span-2">请求地址
              <input value={draft.url} onChange={(event) => setDraft({ ...draft, url: event.target.value })} className="neu-input mt-1 w-full px-3 py-2 font-mono" placeholder={editingId ? `留空保持：${selected?.url_display || ''}` : 'https://example.com/webhook'} />
              {editingId && <span className="mt-1 block text-[11px] text-gray-400">为保护密钥，原地址不回显；留空会保持原地址。</span>}
            </label>
          </div>

          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <FieldEditor title="查询参数" fields={draft.query_params} onChange={(fields) => patchFields('query_params', fields)} />
            <FieldEditor title="请求头" fields={draft.headers} onChange={(fields) => patchFields('headers', fields)} />
          </div>

          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <div>
              <label className="text-xs text-gray-600">Content-Type
                <input value={draft.content_type} onChange={(event) => setDraft({ ...draft, content_type: event.target.value })} className="neu-input mt-1 w-full px-3 py-2 font-mono" />
              </label>
              <label className="mt-3 block text-xs text-gray-600">请求体模板
                <textarea ref={bodyEditor} rows={10} value={draft.body_template} onChange={(event) => setDraft({ ...draft, body_template: event.target.value })} className="neu-input mt-1 w-full px-3 py-2 font-mono text-[11px]" />
              </label>
              <p className="mt-2 text-[11px] text-gray-500">点击变量插入请求体光标处；选中文字时替换选区。变量仅用于请求体模板。</p>
              <div className="mt-2 flex flex-wrap gap-1">
                {HTTP_NOTIFICATION_VARIABLES.map(([name, label]) => (
                  <button key={name} type="button" title={label} onMouseDown={(event) => event.preventDefault()} onClick={() => insertVariable(name)} className="rounded bg-white px-2 py-1 font-mono text-[10px] text-blue-600">
                    {`{{${name}}}`}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <p className="text-xs font-semibold text-gray-700">脱敏预览</p>
              <pre className="mt-1 max-h-[360px] overflow-auto rounded-lg bg-slate-900 p-3 text-[11px] text-slate-100">{buildMaskedPreview({ ...draft, url_display: selected?.url_display })}</pre>
            </div>
          </div>

          <div className="mt-4 flex items-center gap-3">
            <button type="button" disabled={busy !== ''} onClick={() => void save()} className="neu-btn bg-[#52c41a] px-4 py-2 text-xs font-medium text-white disabled:opacity-50">
              {busy === 'save' ? '保存中...' : '保存'}
            </button>
            <span className="text-[11px] text-gray-500">保存后请点“发送测试”；测试成功才可启用。</span>
          </div>
        </div>
      )}
    </section>
  )
}
