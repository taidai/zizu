import { useEffect, useRef, useState } from 'react'
import {
  fetchFaultMaps,
  createFaultMap,
  updateFaultMap,
  deleteFaultMap,
  type FaultMap,
  type FaultMapEntry,
} from '../api/client'
import { restoreModalTrigger, useModalFocus } from './useModalFocus'

export default function FaultMapManager() {
  const [maps, setMaps] = useState<FaultMap[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [editing, setEditing] = useState<FaultMap | null>(null)
  const [showForm, setShowForm] = useState(false)
  const editorTrigger = useRef<HTMLButtonElement | null>(null)
  const [form, setForm] = useState<{ name: string; description: string; entries: FaultMapEntry[] }>({
    name: '',
    description: '',
    entries: [],
  })

  const load = async () => {
    setLoading(true)
    setLoadError('')
    setMaps(null)
    try {
      const data = await fetchFaultMaps()
      setMaps(data.items)
    } catch (reason) {
      setLoadError(`故障映射读取失败：${reason instanceof Error ? reason.message : '未知错误'}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  useEffect(() => {
    if (editing) {
      setForm({
        name: editing.name,
        description: editing.description || '',
        entries: editing.entries?.length ? [...editing.entries] : [],
      })
      setShowForm(true)
    } else {
      setForm({ name: '', description: '', entries: [] })
    }
  }, [editing])

  const handleSave = async () => {
    if (!form.name.trim()) return
    const payload = {
      name: form.name.trim(),
      description: form.description.trim() || null,
      entries: form.entries.filter((e) => e.code.trim() && e.message.trim()),
    }
    try {
      if (editing) {
        await updateFaultMap(editing.id, payload)
      } else {
        await createFaultMap(payload)
      }
      closeEditor()
      await load()
    } catch (e: any) {
      alert('保存失败：' + (e.message || e))
    }
  }

  const handleDelete = async (map: FaultMap) => {
    if (!confirm(`确定永久删除故障码映射表“${map.name}”吗？\n此操作不可恢复，引用该表的点位关联将被清空。`)) return
    try {
      await deleteFaultMap(map.id)
      await load()
    } catch (e: any) {
      alert('删除失败：' + (e.message || e))
    }
  }

  const updateEntry = (idx: number, field: keyof FaultMapEntry, value: string) => {
    const next = [...form.entries]
    next[idx] = { ...next[idx], [field]: value }
    setForm({ ...form, entries: next })
  }

  const addEntry = () => {
    setForm({ ...form, entries: [...form.entries, { code: '', message: '' }] })
  }

  const removeEntry = (idx: number) => {
    const next = [...form.entries]
    next.splice(idx, 1)
    setForm({ ...form, entries: next })
  }

  function closeEditor() {
    setShowForm(false)
    setEditing(null)
    restoreModalTrigger(editorTrigger.current)
  }
  const editorModal = useModalFocus({ open: showForm, onClose: closeEditor })

  const startCreate = (trigger: HTMLButtonElement) => {
    editorTrigger.current = trigger
    setEditing(null)
    setForm({ name: '', description: '', entries: [] })
    setShowForm(true)
  }

  const startEdit = (map: FaultMap, trigger: HTMLButtonElement) => {
    editorTrigger.current = trigger
    setEditing(map)
  }

  return (
    <section className="neu-card p-4" aria-label="故障映射管理">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-bold text-gray-800">故障码映射表</h3>
        <button
          onClick={(event) => startCreate(event.currentTarget)}
          disabled={loading || maps === null}
          className="neu-btn px-3 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d]"
        >
          新建映射表
        </button>
      </div>

      {loading && <div className="text-xs text-gray-400">加载中...</div>}
      {loadError && <div className="flex flex-wrap items-center gap-3"><p role="alert" className="text-xs text-red-700">{loadError}</p><button type="button" onClick={() => void load()} className="neu-btn px-3 text-xs">重试故障映射</button></div>}

      <div className="space-y-2">
        {maps?.map((map) => (
          <div key={map.id} className="bg-gray-50 rounded-lg p-3">
            <div className="flex items-center justify-between">
              <div>
                <div className="text-sm font-medium text-gray-800">{map.name}</div>
                <div className="text-xs text-gray-400">{map.description || '无描述'} · {map.entries?.length || 0} 条映射</div>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={(event) => startEdit(map, event.currentTarget)}
                  className="neu-btn px-3 py-1 text-xs"
                >
                  编辑
                </button>
                <button
                  onClick={() => handleDelete(map)}
                  className="neu-btn px-3 py-1 text-xs text-red-500"
                >
                  删除
                </button>
              </div>
            </div>
            {map.entries && map.entries.length > 0 && (
              <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                {map.entries.slice(0, 6).map((entry, idx) => (
                  <div key={idx} className="neu-inset px-2 py-1 truncate">
                    <span className="font-mono text-gray-600">{entry.code}</span>
                    <span className="mx-1 text-gray-300">→</span>
                    <span className="text-gray-700">{entry.message}</span>
                  </div>
                ))}
                {map.entries.length > 6 && (
                  <div className="text-xs text-gray-400">+{map.entries.length - 6} 条</div>
                )}
              </div>
            )}
          </div>
        ))}
        {maps !== null && maps.length === 0 && !loading && !loadError && (
          <div className="text-center text-gray-400 text-xs py-6">暂无故障码映射表</div>
        )}
      </div>

      {showForm && (
        <div className="zizu-tools-overlay zizu-tools-danger-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) closeEditor() }}>
          <section ref={editorModal.dialogRef} tabIndex={-1} onKeyDown={editorModal.onKeyDown} role="dialog" aria-modal="true" aria-label="故障映射编辑器" className="zizu-tools-dialog zizu-tools-editor-dialog">
            <header className="zizu-tools-dialog-header">
              <div><span>FAULT MAP</span><h2>{editing ? `编辑：${editing.name}` : '新建故障映射'}</h2></div>
              <button type="button" onClick={closeEditor} className="neu-btn zizu-tools-close">关闭</button>
            </header>
            <div className="zizu-tools-dialog-body space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <label className="text-xs text-gray-600">映射表名称<input aria-label="映射表名称" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} className="neu-input mt-1 w-full px-3 py-2 text-xs" /></label>
                <label className="text-xs text-gray-600">描述<input aria-label="映射表描述" value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} className="neu-input mt-1 w-full px-3 py-2 text-xs" /></label>
              </div>
              <div className="space-y-2">
                <div className="flex items-center justify-between"><h3 className="text-xs font-semibold text-gray-700">故障码条目</h3><button type="button" onClick={addEntry} className="neu-btn px-3 text-xs text-gray-600">添加条目</button></div>
                {form.entries.map((entry, idx) => (
                  <div key={idx} className="grid grid-cols-[130px_1fr_auto] items-center gap-2">
                    <input aria-label={`故障码 ${idx + 1}`} value={entry.code} onChange={(event) => updateEntry(idx, 'code', event.target.value)} placeholder="故障码" className="neu-input px-2 py-2 text-xs" />
                    <input aria-label={`故障描述 ${idx + 1}`} value={entry.message} onChange={(event) => updateEntry(idx, 'message', event.target.value)} placeholder="故障描述" className="neu-input px-2 py-2 text-xs" />
                    <button type="button" onClick={() => removeEntry(idx)} className="neu-btn px-3 text-xs text-red-600">移除</button>
                  </div>
                ))}
                {!form.entries.length && <p className="rounded-lg border border-dashed border-gray-300 p-5 text-center text-xs text-gray-400">尚未添加故障码条目。</p>}
              </div>
              <div className="flex justify-end gap-2">
                <button type="button" onClick={closeEditor} className="neu-btn px-4 text-xs">取消</button>
                <button type="button" onClick={() => void handleSave()} className="neu-btn zizu-primary px-4 text-xs font-medium">保存</button>
              </div>
            </div>
          </section>
        </div>
      )}
    </section>
  )
}
