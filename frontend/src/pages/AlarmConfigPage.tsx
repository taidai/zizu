import { useEffect, useMemo, useState } from 'react'
import {
  fetchTags,
  fetchNodes,
  fetchFaultMaps,
  batchUpdateTags,
  fetchAlarmConfig,
  type Tag,
  type Node,
  type FaultMap,
} from '../api/client'

const ALARM_LEVELS = [
  { value: '', label: '不告警' },
  { value: 'error1', label: 'error1（严重）' },
  { value: 'error2', label: 'error2（重要）' },
  { value: 'error3', label: 'error3（一般）' },
]

const LEVEL_STYLES: Record<string, string> = {
  error1: 'bg-red-100 text-red-700 border-red-200',
  error2: 'bg-orange-100 text-orange-700 border-orange-200',
  error3: 'bg-amber-100 text-amber-700 border-amber-200',
}

export default function AlarmConfigPage() {
  const [tags, setTags] = useState<Tag[]>([])
  const [nodes, setNodes] = useState<Node[]>([])
  const [faultMaps, setFaultMaps] = useState<FaultMap[]>([])
  const [configuredTags, setConfiguredTags] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const [nodeFilter, setNodeFilter] = useState('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize] = useState(50)
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(1)

  const [bulkLevel, setBulkLevel] = useState('')
  const [bulkType, setBulkType] = useState('')
  const [bulkThreshold, setBulkThreshold] = useState('')
  const [bulkFaultMapId, setBulkFaultMapId] = useState('')

  const load = async (targetPage = page) => {
    setLoading(true)
    try {
      const [tagsRes, nodesRes, fmRes, cfgRes] = await Promise.all([
        fetchTags(
          nodeFilter || undefined,
          targetPage,
          pageSize,
          search || undefined,
          undefined,
          undefined,
          undefined,
          true,
          'node_id',
          'asc',
        ),
        fetchNodes(),
        fetchFaultMaps(),
        fetchAlarmConfig(),
      ])
      setTags(tagsRes.tags)
      setTotal(tagsRes.total)
      setTotalPages(tagsRes.total_pages)
      setNodes(nodesRes)
      setFaultMaps(fmRes.items)
      setConfiguredTags(new Set(cfgRes.tags.map((t) => t.id)))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load(1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeFilter, search])

  useEffect(() => {
    load(page)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page])

  const toggleSelect = (id: string) => {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelected(next)
  }

  const toggleSelectAll = () => {
    if (selected.size === tags.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(tags.map((t) => t.id)))
    }
  }

  const handleBatchSave = async () => {
    if (selected.size === 0) return
    setSaving(true)
    try {
      const updates: any = {}
      if (bulkLevel !== '') updates.alarm_level = bulkLevel || undefined
      if (bulkType !== '') updates.alarm_type = bulkType || undefined
      if (bulkThreshold !== '') updates.alarm_threshold = bulkThreshold === '' ? undefined : Number(bulkThreshold)
      if (bulkFaultMapId !== '') updates.fault_map_id = bulkFaultMapId || undefined
      if (Object.keys(updates).length === 0) {
        alert('请选择至少一项批量配置')
        return
      }
      await batchUpdateTags(Array.from(selected), updates)
      setSelected(new Set())
      load(page)
    } catch (e: any) {
      alert(e.message || '批量保存失败')
    } finally {
      setSaving(false)
    }
  }

  const handleClearConfig = async () => {
    if (selected.size === 0) return
    if (!confirm(`确定清空 ${selected.size} 个点位的告警配置？`)) return
    setSaving(true)
    try {
      await batchUpdateTags(Array.from(selected), {
        alarm_level: undefined,
        alarm_type: undefined,
        alarm_threshold: undefined,
        fault_map_id: undefined,
      })
      setSelected(new Set())
      load(page)
    } catch (e: any) {
      alert(e.message || '清空失败')
    } finally {
      setSaving(false)
    }
  }

  const nodeName = (nodeId: string) => nodes.find((n) => n.id === nodeId)?.name || nodeId.slice(0, 8)

  const faultMapName = (id: string | null) => {
    if (!id) return null
    return faultMaps.find((fm) => fm.id === id)?.name || id.slice(0, 8)
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-bold text-gray-800">点位告警配置</h2>
          <p className="text-xs text-gray-500">批量为 tag 配置 error1/error2/error3 分级告警、阈值与故障码映射表。</p>
        </div>
        <div className="text-xs text-gray-500">
          已配置 <span className="font-mono font-bold text-gray-700">{configuredTags.size}</span> 个点位
        </div>
      </div>

      {/* Filters */}
      <div className="neu-card p-3 flex items-center gap-3">
        <select
          value={nodeFilter}
          onChange={(e) => { setNodeFilter(e.target.value); setPage(1) }}
          className="neu-input px-3 py-1.5 text-xs bg-transparent min-w-[140px]"
        >
          <option value="">全部节点</option>
          {nodes.map((n) => (
            <option key={n.id} value={n.id}>{n.name}</option>
          ))}
        </select>
        <input
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1) }}
          placeholder="搜索点位名..."
          className="neu-input flex-1 px-3 py-1.5 text-xs"
        />
      </div>

      {/* Bulk actions */}
      <div className="neu-card p-3 space-y-3">
        <div className="flex items-center gap-2 text-xs text-gray-600">
          <span className="font-medium">批量配置</span>
          <span className="text-gray-400">已选 {selected.size} 个</span>
        </div>
        <div className="grid grid-cols-12 gap-3">
          <div className="col-span-2">
            <label className="block text-[10px] text-gray-500 mb-1">告警等级</label>
            <select
              value={bulkLevel}
              onChange={(e) => setBulkLevel(e.target.value)}
              className="neu-input w-full px-2 py-1.5 text-xs bg-transparent"
            >
              <option value="">保持不变</option>
              {ALARM_LEVELS.map((l) => (
                <option key={l.value} value={l.value}>{l.label}</option>
              ))}
            </select>
          </div>
          <div className="col-span-2">
            <label className="block text-[10px] text-gray-500 mb-1">告警类型</label>
            <input
              value={bulkType}
              onChange={(e) => setBulkType(e.target.value)}
              placeholder="如：过温"
              className="neu-input w-full px-2 py-1.5 text-xs"
            />
          </div>
          <div className="col-span-2">
            <label className="block text-[10px] text-gray-500 mb-1">阈值</label>
            <input
              type="number"
              value={bulkThreshold}
              onChange={(e) => setBulkThreshold(e.target.value)}
              placeholder="留空即激活模式"
              className="neu-input w-full px-2 py-1.5 text-xs"
            />
          </div>
          <div className="col-span-4">
            <label className="block text-[10px] text-gray-500 mb-1">故障码映射表</label>
            <select
              value={bulkFaultMapId}
              onChange={(e) => setBulkFaultMapId(e.target.value)}
              className="neu-input w-full px-2 py-1.5 text-xs bg-transparent"
            >
              <option value="">保持不变</option>
              <option value="__clear__">清空映射表</option>
              {faultMaps.map((fm) => (
                <option key={fm.id} value={fm.id}>{fm.name}</option>
              ))}
            </select>
          </div>
          <div className="col-span-2 flex items-end gap-2">
            <button
              onClick={handleBatchSave}
              disabled={selected.size === 0 || saving}
              className="neu-btn px-3 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50"
            >
              {saving ? '保存中...' : '应用'}
            </button>
            <button
              onClick={handleClearConfig}
              disabled={selected.size === 0 || saving}
              className="neu-btn px-3 py-1.5 text-xs text-red-600 hover:bg-red-50 disabled:opacity-50"
            >
              清空
            </button>
          </div>
        </div>
      </div>

      {/* Table */}
      <div className="neu-card p-0 overflow-hidden">
        <div className="max-h-[calc(100vh-380px)] overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="bg-gray-100/60 text-gray-600 sticky top-0">
              <tr>
                <th className="px-3 py-2 text-left w-10">
                  <input
                    type="checkbox"
                    checked={tags.length > 0 && selected.size === tags.length}
                    onChange={toggleSelectAll}
                    className="w-4 h-4 accent-[#52c41a]"
                  />
                </th>
                <th className="px-3 py-2 text-left">节点</th>
                <th className="px-3 py-2 text-left">点位名</th>
                <th className="px-3 py-2 text-left">数据类型</th>
                <th className="px-3 py-2 text-left">告警等级</th>
                <th className="px-3 py-2 text-left">类型/阈值</th>
                <th className="px-3 py-2 text-left">故障码映射</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {loading && tags.length === 0 && (
                <tr><td colSpan={7} className="px-3 py-8 text-center text-gray-400">加载中...</td></tr>
              )}
              {tags.map((t) => {
                const isSelected = selected.has(t.id)
                return (
                  <tr
                    key={t.id}
                    onClick={() => toggleSelect(t.id)}
                    className={`cursor-pointer hover:bg-white/40 ${isSelected ? 'bg-[#52c41a]/5' : ''}`}
                  >
                    <td className="px-3 py-2">
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => toggleSelect(t.id)}
                        className="w-4 h-4 accent-[#52c41a]"
                      />
                    </td>
                    <td className="px-3 py-2 text-gray-600">{nodeName(t.node_id)}</td>
                    <td className="px-3 py-2">
                      <div className="font-medium text-gray-700">{t.display_name || t.name}</div>
                      <div className="text-[10px] text-gray-400 font-mono">{t.name}</div>
                    </td>
                    <td className="px-3 py-2 text-gray-500">{t.data_type}</td>
                    <td className="px-3 py-2">
                      {t.alarm_level ? (
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium border ${LEVEL_STYLES[t.alarm_level] || 'bg-gray-100 text-gray-600'}`}>
                          {t.alarm_level}
                        </span>
                      ) : (
                        <span className="text-gray-400">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-gray-500">
                      {t.alarm_type || ''}
                      {t.alarm_threshold !== null && t.alarm_threshold !== undefined ? ` ≥ ${t.alarm_threshold}` : ''}
                    </td>
                    <td className="px-3 py-2 text-gray-500">
                      {faultMapName(t.fault_map_id) || <span className="text-gray-400">—</span>}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        <div className="px-3 py-2 border-t border-gray-100 flex items-center justify-between text-xs text-gray-500">
          <div>
            第 {page}/{totalPages} 页，共 {total} 条
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1}
              className="neu-btn px-2 py-1 text-xs disabled:opacity-50"
            >
              上一页
            </button>
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              className="neu-btn px-2 py-1 text-xs disabled:opacity-50"
            >
              下一页
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
