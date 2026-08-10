import { useEffect, useMemo, useRef, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import {
  fetchEntities,
  fetchEntity,
  createEntity,
  updateEntity,
  deleteEntity,
  bindTagToEntity,
  unbindTagFromEntity,
  fetchEntityRealtime,
  fetchEntityHistory,
  fetchTags,
  fetchNodes,
  type Entity,
  type EntityBinding,
  type Tag,
  type Node,
  exportEntitiesCsv,
  exportEntitiesJson,
  importEntitiesFile,
} from '../api/client'

const DATA_TYPES = ['FLOAT', 'INT', 'BOOL', 'STRING', 'ENUM']
const ENTITY_TYPES = [
  { key: 'R', label: '只读 R' },
  { key: 'W', label: '只写 W' },
  { key: 'RW', label: '读写 RW' },
]

export default function EntityManagerPage() {
  const [entities, setEntities] = useState<Entity[]>([])
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<Entity | null>(null)
  const [detail, setDetail] = useState<(Entity & { bindings: EntityBinding[] }) | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<Entity | null>(null)
  const [tags, setTags] = useState<Tag[]>([])
  const [nodes, setNodes] = useState<Node[]>([])
  const [realtime, setRealtime] = useState<any>(null)
  const [activeTab, setActiveTab] = useState<'bindings' | 'realtime' | 'history'>('bindings')
  const [historyRange, setHistoryRange] = useState<'1h' | '24h' | '7d'>('1h')
  const [historyPoints, setHistoryPoints] = useState<{ ts: string; value: number | string | boolean | null; quality: number }[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [importing, setImporting] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const load = async () => {
    setLoading(true)
    try {
      const data = await fetchEntities({ search, page_size: 200 })
      setEntities(data.items)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [search])

  useEffect(() => {
    if (selected) {
      fetchEntity(selected.id).then(setDetail)
      fetchEntityRealtime(selected.id).then(setRealtime).catch(() => setRealtime(null))
      setActiveTab('bindings')
    } else {
      setDetail(null)
      setRealtime(null)
      setHistoryPoints([])
    }
  }, [selected])

  useEffect(() => {
    if (selected && activeTab === 'history') {
      setHistoryLoading(true)
      fetchEntityHistory(selected.id, historyRange)
        .then((data) => setHistoryPoints(data.points || []))
        .catch(() => setHistoryPoints([]))
        .finally(() => setHistoryLoading(false))
    }
  }, [selected, activeTab, historyRange])

  useEffect(() => {
    Promise.all([fetchTags(undefined, 1, 200, undefined, undefined, undefined, undefined, true), fetchNodes()]).then(([t, n]) => {
      setTags(t.tags)
      setNodes(n)
    })
  }, [])

  const categories = useMemo(() => {
    const set = new Set<string>()
    entities.forEach((e) => { if (e.category) set.add(e.category) })
    return Array.from(set).sort()
  }, [entities])

  const handleCreate = async (form: any) => {
    await createEntity(form)
    setShowForm(false)
    load()
  }

  const handleUpdate = async (form: any) => {
    if (!editing) return
    await updateEntity(editing.id, form)
    setEditing(null)
    load()
    if (selected?.id === editing.id) fetchEntity(editing.id).then(setDetail)
  }

  const handleDelete = async (id: string) => {
    if (!confirm('确定删除该实体？绑定关系会一并删除。')) return
    await deleteEntity(id)
    if (selected?.id === id) setSelected(null)
    load()
  }

  const handleBind = async (form: Omit<EntityBinding, 'id' | 'entity_id' | 'tag_name' | 'tag_display_name' | 'node_name'>) => {
    if (!selected) return
    await bindTagToEntity(selected.id, form)
    fetchEntity(selected.id).then(setDetail)
  }

  const handleUnbind = async (bindingId: string) => {
    if (!selected || !confirm('确定解除绑定？')) return
    await unbindTagFromEntity(selected.id, bindingId)
    fetchEntity(selected.id).then(setDetail)
  }

  const handleImport = async (file: File) => {
    setImporting(true)
    try {
      const r = await importEntitiesFile(file, 'upsert', false)
      alert(`导入完成：新建 ${r.created}，更新 ${r.updated}，跳过 ${r.skipped}${r.errors.length ? `，错误 ${r.errors.length} 条` : ''}`)
      load()
    } catch (e: any) {
      alert('导入失败：' + (e?.message || e))
    } finally {
      setImporting(false)
    }
  }

  return (
    <div className="min-h-0 flex gap-4">
      {/* 左侧列表 */}
      <div className="w-1/3 neu-card p-4 flex flex-col min-h-0">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-bold text-gray-800">全局实体</h2>
          <div className="flex items-center gap-1.5">
            <button onClick={() => exportEntitiesCsv()} className="neu-btn px-2.5 py-1.5 text-xs">导出CSV</button>
            <button onClick={() => exportEntitiesJson()} className="neu-btn px-2.5 py-1.5 text-xs">导出JSON</button>
            <button onClick={() => fileInputRef.current?.click()} disabled={importing} className="neu-btn px-2.5 py-1.5 text-xs">{importing ? '导入中...' : '导入'}</button>
            <button onClick={() => setShowForm(true)} className="neu-btn px-3 py-1.5 text-xs bg-[#52c41a] text-white">新建</button>
            <input ref={fileInputRef} type="file" accept=".csv,.json,application/json,text/csv" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) handleImport(f); e.target.value = '' }} />
          </div>
        </div>
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="搜索实体名..."
          className="neu-inset w-full px-3 py-2 text-xs mb-3"
        />
        <div className="flex-1 overflow-y-auto space-y-2">
          {loading && <div className="text-xs text-gray-400">加载中...</div>}
          {entities.map((e) => (
            <div
              key={e.id}
              onClick={() => setSelected(e)}
              className={`p-3 rounded-xl cursor-pointer transition ${
                selected?.id === e.id
                  ? 'bg-[#52c41a] text-white shadow'
                  : 'bg-white/40 hover:bg-white/60 text-gray-700'
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="text-sm font-bold">{e.display_name || e.name}</span>
                {!e.enabled && <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-500/20">禁用</span>}
              </div>
              <div className={`text-[10px] mt-0.5 ${selected?.id === e.id ? 'text-white/80' : 'text-gray-400'}`}>
                {e.name} · {e.entity_type} · {e.category || '无分类'}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* 右侧详情 */}
      {selected && detail && (
        <div className="w-2/3 neu-card p-4 flex flex-col min-h-0">
          <div className="flex items-start justify-between mb-3">
            <div>
              <h3 className="text-base font-bold text-gray-800">{detail.display_name || detail.name}</h3>
              <p className="text-xs text-gray-500 font-mono">{detail.name}</p>
            </div>
            <div className="flex items-center gap-2">
              <button onClick={() => setEditing(detail)} className="neu-btn px-3 py-1.5 text-xs text-gray-600">编辑</button>
              {!detail.is_system && (
                <button onClick={() => handleDelete(detail.id)} className="neu-btn px-3 py-1.5 text-xs text-red-500 hover:bg-red-50">删除</button>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2 mb-3 border-b border-gray-200 pb-2">
            {[
              { key: 'bindings', label: '点位绑定' },
              { key: 'realtime', label: '实时数据' },
              { key: 'history', label: '历史数据' },
            ].map((t) => (
              <button
                key={t.key}
                onClick={() => setActiveTab(t.key as any)}
                className={`px-3 py-1.5 text-xs font-medium rounded-t ${activeTab === t.key ? 'bg-[#52c41a] text-white' : 'text-gray-600 hover:bg-gray-100'}`}
              >
                {t.label}
              </button>
            ))}
          </div>

          {activeTab === 'realtime' && (
            <div className="neu-card p-3">
              <div className="text-[10px] text-gray-400 uppercase">实时值</div>
              {realtime ? (
                <div className="mt-1">
                  <div className="text-2xl font-bold text-gray-800 font-mono-value">
                    {realtime.value ?? '—'} {detail.unit || ''}
                  </div>
                  <div className="text-[10px] text-gray-400 mt-0.5">
                    绑定: {realtime.tag_name} @ {realtime.node_name} · {realtime.ts ? new Date(realtime.ts).toLocaleString() : '—'}
                  </div>
                </div>
              ) : (
                <div className="text-sm text-gray-400">暂无实时数据</div>
              )}
            </div>
          )}

          {activeTab === 'history' && (
            <div className="flex-1 flex flex-col min-h-0">
              <div className="flex items-center gap-2 mb-2">
                {(['1h', '24h', '7d'] as const).map((r) => (
                  <button
                    key={r}
                    onClick={() => setHistoryRange(r)}
                    className={`neu-btn px-3 py-1 text-xs ${historyRange === r ? 'bg-[#52c41a] text-white' : 'text-gray-600'}`}
                  >
                    {r === '1h' ? '1小时' : r === '24h' ? '24小时' : '7天'}
                  </button>
                ))}
              </div>
              <div className="flex-1 min-h-[250px]">
                {historyLoading ? (
                  <div className="h-full flex items-center justify-center text-gray-400 text-sm">加载中...</div>
                ) : historyPoints.length === 0 ? (
                  <div className="h-full flex items-center justify-center text-gray-400 text-sm">暂无历史数据</div>
                ) : (
                  <ReactECharts
                    option={{
                      backgroundColor: 'transparent',
                      animation: false,
                      grid: { left: 60, right: 20, top: 20, bottom: 30 },
                      tooltip: {
                        trigger: 'axis',
                        backgroundColor: 'rgba(255,255,255,0.95)',
                        borderColor: '#d1d9e6',
                        textStyle: { color: '#333', fontSize: 12 },
                        formatter: (params: any) => {
                          const p = params[0]
                          const d = new Date(p.axisValue)
                          return `<div style="font-family:monospace">${d.toLocaleString()}</div>
                            <div style="color:#389e0d;font-weight:bold">${detail.name}: ${p.data ?? '—'} ${detail.unit || ''}</div>`
                        },
                      },
                      xAxis: {
                        type: 'time',
                        axisLine: { lineStyle: { color: '#d1d9e6' } },
                        axisLabel: { color: '#666', fontSize: 11 },
                        splitLine: { show: false },
                      },
                      yAxis: {
                        type: 'value',
                        name: detail.unit || '',
                        nameTextStyle: { color: '#888', fontSize: 11 },
                        axisLine: { show: false },
                        axisLabel: { color: '#666', fontSize: 11 },
                        splitLine: { lineStyle: { color: '#e8ecf1', type: 'dashed' } },
                      },
                      series: [{
                        name: detail.name,
                        type: 'line',
                        showSymbol: false,
                        smooth: true,
                        lineStyle: { color: '#52c41a', width: 2 },
                        areaStyle: {
                          color: {
                            type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
                            colorStops: [
                              { offset: 0, color: 'rgba(82,196,26,0.25)' },
                              { offset: 1, color: 'rgba(82,196,26,0.02)' },
                            ],
                          },
                        },
                        data: historyPoints.map((p) => [p.ts, p.value]),
                      }],
                    }}
                    style={{ height: '100%', width: '100%' }}
                  />
                )}
              </div>
            </div>
          )}

          {activeTab === 'bindings' && (
            <>
              <div className="flex items-center justify-between mb-2">
                <h4 className="text-sm font-bold text-gray-700">点位绑定</h4>
                <EntityBindForm tags={tags} nodes={nodes} onBind={handleBind} />
              </div>
              <div className="flex-1 overflow-y-auto">
                {detail.bindings.length === 0 ? (
                  <div className="text-xs text-gray-400">暂无绑定</div>
                ) : (
                  <table className="w-full text-xs">
                    <thead className="text-[10px] text-gray-400 uppercase border-b border-gray-200">
                      <tr>
                        <th className="text-left py-1.5">点位</th>
                        <th className="text-left py-1.5">节点</th>
                        <th className="text-left py-1.5">类型</th>
                        <th className="text-left py-1.5">品牌</th>
                        <th className="text-left py-1.5">优先级</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.bindings.map((b) => (
                        <tr key={b.id} className="border-b border-gray-100 last:border-0">
                          <td className="py-1.5">{b.tag_display_name || b.tag_name}</td>
                          <td className="py-1.5 text-gray-500">{b.node_name}</td>
                          <td className="py-1.5"><span className={`px-1.5 py-0.5 rounded text-[10px] ${b.binding_type === 'PHYSICAL' ? 'bg-emerald-100 text-emerald-700' : 'bg-indigo-100 text-indigo-700'}`}>{b.binding_type}</span></td>
                          <td className="py-1.5 text-gray-500">{b.brand || '—'}</td>
                          <td className="py-1.5">{b.priority}</td>
                          <td className="py-1.5 text-right"><button onClick={() => handleUnbind(b.id)} className="text-red-500 hover:underline text-[10px]">解绑</button></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </>
          )}
        </div>
      )}

      {showForm && (
        <EntityForm
          categories={categories}
          onClose={() => { setShowForm(false); setEditing(null) }}
          onSubmit={editing ? handleUpdate : handleCreate}
        />
      )}
      {editing && (
        <EntityForm
          categories={categories}
          initial={editing}
          onClose={() => { setShowForm(false); setEditing(null) }}
          onSubmit={editing ? handleUpdate : handleCreate}
        />
      )}
    </div>
  )
}

function EntityForm({ categories, initial, onClose, onSubmit }: any) {
  const [form, setForm] = useState({
    name: initial?.name || '',
    display_name: initial?.display_name || '',
    entity_type: initial?.entity_type || 'R',
    data_type: initial?.data_type || 'FLOAT',
    unit: initial?.unit || '',
    category: initial?.category || '',
    description: initial?.description || '',
    enabled: initial?.enabled ?? true,
  })

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="neu-card p-5 w-[480px] max-h-[90vh] overflow-y-auto">
        <h3 className="text-base font-bold mb-4">{initial ? `编辑实体${initial.is_system ? '（系统内置）' : ''}` : '新建实体'}</h3>
        <div className="space-y-3">
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="实体全局名，如 pcs.activePower" className="neu-inset w-full px-3 py-2 text-xs" disabled={!!initial} />
          <input value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} placeholder="显示名" className="neu-inset w-full px-3 py-2 text-xs" />
          <div className="grid grid-cols-2 gap-3">
            <select value={form.entity_type} onChange={(e) => setForm({ ...form, entity_type: e.target.value as any })} className="neu-inset w-full px-3 py-2 text-xs" disabled={initial?.is_system}>
              {ENTITY_TYPES.map((t) => <option key={t.key} value={t.key}>{t.label}</option>)}
            </select>
            <select value={form.data_type} onChange={(e) => setForm({ ...form, data_type: e.target.value })} className="neu-inset w-full px-3 py-2 text-xs" disabled={initial?.is_system}>
              {DATA_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <input value={form.unit} onChange={(e) => setForm({ ...form, unit: e.target.value })} placeholder="单位" className="neu-inset w-full px-3 py-2 text-xs" />
          <input value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} placeholder="分类，如 pcs / bms / meter" list="cat-list" className="neu-inset w-full px-3 py-2 text-xs" disabled={initial?.is_system} />
          <datalist id="cat-list">{categories.map((c: string) => <option key={c} value={c} />)}</datalist>
          <textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="描述" className="neu-inset w-full px-3 py-2 text-xs h-16" />
          <label className="flex items-center gap-2 text-xs">
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /> 启用
          </label>
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <button onClick={onClose} className="neu-btn px-4 py-2 text-xs">取消</button>
          <button onClick={() => onSubmit(form)} className="neu-btn px-4 py-2 text-xs bg-[#52c41a] text-white">保存</button>
        </div>
      </div>
    </div>
  )
}

function EntityBindForm({ tags, nodes, onBind }: any) {
  const [tagId, setTagId] = useState('')
  const [nodeId, setNodeId] = useState('')
  const [bindingType, setBindingType] = useState<'PHYSICAL' | 'VIRTUAL'>('PHYSICAL')
  const [brand, setBrand] = useState('')
  const [priority, setPriority] = useState(1)

  const selectedTag = tags.find((t: Tag) => t.id === tagId)

  useEffect(() => {
    if (selectedTag) setNodeId(selectedTag.node_id)
  }, [tagId, selectedTag])

  const submit = () => {
    if (!tagId || !nodeId) return
    onBind({ tag_id: tagId, node_id: nodeId, binding_type: bindingType, brand: brand || undefined, priority })
    setTagId('')
    setBrand('')
    setPriority(1)
  }

  return (
    <div className="flex items-center gap-2">
      <select value={tagId} onChange={(e) => setTagId(e.target.value)} className="neu-inset px-2 py-1.5 text-xs max-w-[160px]">
        <option value="">选择点位</option>
        {tags.map((t: Tag) => <option key={t.id} value={t.id}>{t.display_name || t.name} ({t.node_name})</option>)}
      </select>
      <select value={bindingType} onChange={(e) => setBindingType(e.target.value as any)} className="neu-inset px-2 py-1.5 text-xs">
        <option value="PHYSICAL">物理</option>
        <option value="VIRTUAL">虚拟</option>
      </select>
      <input value={brand} onChange={(e) => setBrand(e.target.value)} placeholder="品牌" className="neu-inset px-2 py-1.5 text-xs w-20" />
      <input type="number" value={priority} onChange={(e) => setPriority(parseInt(e.target.value || '1'))} placeholder="优先级" className="neu-inset px-2 py-1.5 text-xs w-16" />
      <button onClick={submit} className="neu-btn px-3 py-1.5 text-xs bg-[#52c41a] text-white">绑定</button>
    </div>
  )
}
