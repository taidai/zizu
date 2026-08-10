import { useCallback, useEffect, useState } from 'react'
import {
  fetchTags, updateTag, batchUpdateTags, deleteTag, createTag, fetchNodes, fetchAlarmCounts, connectTelemetryWS, fetchFaultMaps,
  fetchAlarmTypes,
  type Tag, type TelemetryUpdate, type TagCreateInput, type FaultMap,
} from '../api/client'
import EditableCell from './EditableCell'
import TrendChart from './TrendChart'
import type { Node } from '../api/client'

const SORTABLE_COLUMNS = [
  { key: 'name', label: '点位名' },
  { key: 'data_type', label: '类型' },
  { key: 'tag_type', label: '点位类型' },
  { key: 'unit', label: '单位' },
  { key: 'raw_value', label: '原始值' },
  { key: 'eng_value', label: '工程值' },
  { key: 'scale_factor', label: 'Scale' },
  { key: 'value_offset', label: 'Offset' },
  { key: 'quality', label: '质量' },
  { key: 'latest_ts', label: '最后更新' },
] as const

interface NodeTagPanelProps {
  nodeId: string
}

export default function NodeTagPanel({ nodeId }: NodeTagPanelProps) {
  const [tags, setTags] = useState<Tag[]>([])
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [dataType, setDataType] = useState('')
  const [tagType, setTagType] = useState('')
  const [readWrite, setReadWrite] = useState('')
  const [showDisabled, setShowDisabled] = useState(false)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(1)
  const [sortBy, setSortBy] = useState('sort_order')
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('asc')
  const [realtimeValues, setRealtimeValues] = useState<Map<string, TelemetryUpdate>>(new Map())
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [batchScale, setBatchScale] = useState('')
  const [batchOffset, setBatchOffset] = useState('')
  const [batchUnit, setBatchUnit] = useState('')
  const [batchReadWrite, setBatchReadWrite] = useState('')
  const [batchEnabled, setBatchEnabled] = useState<boolean | ''>('')
  const [batchTargetNode, setBatchTargetNode] = useState('')
  const [nodes, setNodes] = useState<Node[]>([])
  const [faultMaps, setFaultMaps] = useState<FaultMap[]>([])
  const [batchAlarmLevel, setBatchAlarmLevel] = useState<'error1' | 'error2' | 'error3' | ''>('')
  const [batchFaultMapId, setBatchFaultMapId] = useState<string>('__keep__')
  const [batchAlarmType, setBatchAlarmType] = useState<string>('')
  const [batchAlarmThreshold, setBatchAlarmThreshold] = useState('')
  const [alarmTypes, setAlarmTypes] = useState<string[]>([])
  const [batchSaving, setBatchSaving] = useState(false)
  const [trendTag, setTrendTag] = useState<Tag | null>(null)
  const [editingTag, setEditingTag] = useState<Tag | null>(null)
  const [showCreateModal, setShowCreateModal] = useState(false)
  const pageSize = 50

  const loadTags = useCallback(async () => {
    setLoading(true)
    try {
      const data = await fetchTags(
        nodeId,
        page,
        pageSize,
        search || undefined,
        dataType || undefined,
        tagType || undefined,
        readWrite || undefined,
        showDisabled ? undefined : true,
        sortBy,
        sortOrder,
      )
      setTags(data.tags)
      setTotal(data.total)
      setTotalPages(data.total_pages || 1)
    } finally {
      setLoading(false)
    }
  }, [nodeId, page, search, dataType, sortBy, sortOrder])

  useEffect(() => {
    setPage(1)
  }, [nodeId, search, dataType, tagType, readWrite, showDisabled])

  useEffect(() => {
    fetchNodes().then(setNodes).catch(() => {})
    fetchFaultMaps().then((d) => setFaultMaps(d.items)).catch(() => {})
  }, [])

  useEffect(() => {
    fetchAlarmTypes().then(setAlarmTypes).catch(() => {})
  }, [])
  useEffect(() => {
    loadTags()
  }, [loadTags])

  useEffect(() => {
    const tagIds = tags.map((t) => t.id)
    if (tagIds.length === 0) return
    const cleanup = connectTelemetryWS((updates) => {
      setRealtimeValues((prev) => {
        const next = new Map(prev)
        for (const u of updates) {
          next.set(u.tag_id, u)
        }
        return next
      })
    }, tagIds)
    return cleanup
  }, [tags])

  const handleSort = (column: string) => {
    if (sortBy === column) {
      setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc')
    } else {
      setSortBy(column)
      setSortOrder('asc')
    }
  }

  const handleUpdateScale = async (tagId: string, v: number) => {
    await updateTag(tagId, { scale_factor: v })
    loadTags()
  }

  const handleUpdateOffset = async (tagId: string, v: number) => {
    await updateTag(tagId, { value_offset: v })
    loadTags()
  }

  const toggleAll = () => {
    if (tags.length > 0 && tags.every((t) => selectedIds.has(t.id))) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(tags.map((t) => t.id)))
    }
  }

  const toggleOne = (id: string) => {
    const next = new Set(selectedIds)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelectedIds(next)
  }

  const handleBatchApply = async () => {
    const scale = batchScale ? parseFloat(batchScale) : undefined
    const offset = batchOffset ? parseFloat(batchOffset) : undefined
    const updates: any = {}
    if (scale !== undefined) updates.scale_factor = scale
    if (offset !== undefined) updates.value_offset = offset
    if (batchUnit !== '') updates.unit = batchUnit
    if (batchReadWrite !== '') updates.read_write = batchReadWrite
    if (batchEnabled !== '') updates.enabled = batchEnabled
    if (batchTargetNode !== '') updates.node_id = batchTargetNode

    if (batchAlarmLevel !== '') updates.alarm_level = batchAlarmLevel
    if (batchFaultMapId !== '__keep__') updates.fault_map_id = batchFaultMapId === '__clear__' ? '' : batchFaultMapId

    if (Object.keys(updates).length === 0) {
      alert('请至少选择一项批量操作')
      return
    }
    setBatchSaving(true)
    try {
      await batchUpdateTags(Array.from(selectedIds), updates)
      setSelectedIds(new Set())
      setBatchScale('')
      setBatchOffset('')
      setBatchUnit('')
             setBatchReadWrite('')
             setBatchEnabled('')
             setBatchTargetNode('')
              setBatchAlarmLevel('')
              setBatchFaultMapId('__keep__')
      setBatchAlarmLevel('')
      setBatchFaultMapId('__keep__')
      loadTags()
    } catch {
      alert('批量更新失败')
    } finally {
      setBatchSaving(false)
    }
  }

  const allSelected = tags.length > 0 && tags.every((t) => selectedIds.has(t.id))
  const someSelected = tags.some((t) => selectedIds.has(t.id))

  const handleDeleteTag = async (tagId: string) => {
    if (!confirm('确定删除该点位？相关历史数据将一并清除。')) return
    try {
      await deleteTag(tagId)
      loadTags()
    } catch (e: any) {
      alert('删除失败: ' + (e.message || e))
    }
  }

  const handleBatchDelete = async () => {
    if (!confirm(`确定删除选中的 ${selectedIds.size} 个点位？相关历史数据将一并清除。`)) return
    setBatchSaving(true)
    try {
      await Promise.all(Array.from(selectedIds).map((id) => deleteTag(id)))
      setSelectedIds(new Set())
      loadTags()
    } catch (e: any) {
      alert('批量删除失败: ' + (e.message || e))
    } finally {
      setBatchSaving(false)
    }
  }

  const handleSaveTag = async (form: Partial<TagCreateInput>) => {
    try {
      if (editingTag) {
        await updateTag(editingTag.id, form)
      } else {
        await createTag({ node_id: nodeId, ...form } as any)
      }
      setEditingTag(null)
      setShowCreateModal(false)
      loadTags()
    } catch (e: any) {
      alert('保存失败: ' + (e.message || e))
    }
  }

  const formatTs = (ts: string | null) => {
    if (!ts) return '—'
    const d = new Date(ts)
    const now = new Date()
    const diffSec = Math.floor((now.getTime() - d.getTime()) / 1000)
    if (diffSec < 60) return '刚刚'
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`
    return d.toLocaleString('zh-CN')
  }

  const isOnline = (tag: Tag) => {
    if (tag.quality === undefined || tag.quality === null) return false
    return tag.quality >= 192
  }

  return (
    <div className="space-y-3">
      {/* 工具栏 */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <label className="text-xs font-medium text-gray-600 whitespace-nowrap">类型:</label>
          <select
            value={dataType}
            onChange={(e) => setDataType(e.target.value)}
            className="neu-input px-3 py-1.5 text-xs bg-transparent min-w-[100px]"
          >
            <option value="">全部</option>
            <option value="FLOAT">FLOAT</option>
            <option value="INT">INT</option>
            <option value="BOOL">BOOL</option>
            <option value="STRING">STRING</option>
          </select>
        </div>

        <div className="flex items-center gap-2">
          <label className="text-xs font-medium text-gray-600 whitespace-nowrap">搜索:</label>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="点位名 / 显示名..."
            className="neu-input px-3 py-1.5 text-xs bg-transparent min-w-[160px]"
          />
        </div>

        <button
          onClick={loadTags}
          disabled={loading}
          className="neu-btn px-4 py-1.5 text-xs font-medium text-[#389e0d] disabled:opacity-50"
        >
          {loading ? '加载中...' : '刷新'}
        </button>

        <div className="ml-auto flex items-center gap-2 text-xs text-gray-500">
          <button
            onClick={() => setShowCreateModal(true)}
            className="neu-btn px-3 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d]"
          >
            + 新建点位
          </button>
          <span>共 {total} 个点位</span>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1 || loading}
              className="neu-btn w-7 h-7 flex items-center justify-center disabled:opacity-30"
            >
              ‹
            </button>
            <span className="px-2 font-mono">
              {page} / {totalPages}
            </span>
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages || loading}
              className="neu-btn w-7 h-7 flex items-center justify-center disabled:opacity-30"
            >
              ›
            </button>
          </div>
        </div>
      </div>

      {/* 高级过滤 */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <label className="text-xs font-medium text-gray-600 whitespace-nowrap">点位类型:</label>
          <select
            value={tagType}
            onChange={(e) => setTagType(e.target.value)}
            className="neu-input px-3 py-1.5 text-xs bg-transparent min-w-[100px]"
          >
            <option value="">全部</option>
            <option value="PHYSICAL">PHYSICAL</option>
            <option value="LOGICAL">LOGICAL</option>
          </select>
        </div>

        <div className="flex items-center gap-2">
          <label className="text-xs font-medium text-gray-600 whitespace-nowrap">读写:</label>
          <select
            value={readWrite}
            onChange={(e) => setReadWrite(e.target.value)}
            className="neu-input px-3 py-1.5 text-xs bg-transparent min-w-[80px]"
          >
            <option value="">全部</option>
            <option value="R">R</option>
            <option value="RW">RW</option>
            <option value="W">W</option>
          </select>
        </div>

        <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
          <input
            type="checkbox"
            checked={showDisabled}
            onChange={(e) => setShowDisabled(e.target.checked)}
            className="w-4 h-4 accent-[#52c41a]"
          />
          包含已禁用
        </label>
      </div>

      {/* 批量编辑 */}
      {selectedIds.size > 0 && (
        <div className="neu-card p-3 flex flex-wrap items-center gap-4 bg-[#52c41a]/5 border border-[#52c41a]/20">
          <span className="text-xs font-medium text-gray-700">
            已选 <span className="text-[#389e0d] font-bold">{selectedIds.size}</span> 个点位
          </span>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-600">Scale:</label>
            <input
              type="number"
              step="any"
              value={batchScale}
              onChange={(e) => setBatchScale(e.target.value)}
              placeholder="统一 Scale"
              className="neu-input px-2 py-1 text-xs w-24"
            />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-600">Offset:</label>
            <input
              type="number"
              step="any"
              value={batchOffset}
              onChange={(e) => setBatchOffset(e.target.value)}
              placeholder="统一 Offset"
              className="neu-input px-2 py-1 text-xs w-24"
            />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-600">单位:</label>
            <input
              type="text"
              value={batchUnit}
              onChange={(e) => setBatchUnit(e.target.value)}
              placeholder="统一单位"
              className="neu-input px-2 py-1 text-xs w-24"
            />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-600">读写:</label>
            <select
              value={batchReadWrite}
              onChange={(e) => setBatchReadWrite(e.target.value)}
              className="neu-input px-2 py-1 text-xs bg-transparent w-20"
            >
              <option value="">不变</option>
              <option value="R">R</option>
              <option value="RW">RW</option>
              <option value="W">W</option>
            </select>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-600">启用:</label>
            <select
              value={batchEnabled === '' ? '' : String(batchEnabled)}
              onChange={(e) => {
                const v = e.target.value
                setBatchEnabled(v === '' ? '' : v === 'true')
              }}
              className="neu-input px-2 py-1 text-xs bg-transparent w-24"
            >
              <option value="">不变</option>
              <option value="true">启用</option>
              <option value="false">禁用</option>
            </select>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-600">移动到:</label>
            <select
              value={batchTargetNode}
              onChange={(e) => setBatchTargetNode(e.target.value)}
              className="neu-input px-2 py-1 text-xs bg-transparent min-w-[120px]"
            >
              <option value="">不移动</option>
              {nodes.map((n) => (
                <option key={n.id} value={n.id}>{n.name}</option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-600">告警级别:</label>
            <select
              value={batchAlarmLevel}
              onChange={(e) => setBatchAlarmLevel(e.target.value as any)}
              className="neu-input px-2 py-1 text-xs bg-transparent w-28"
            >
              <option value="">不变</option>
              <option value="error1">error1 (CRITICAL)</option>
              <option value="error2">error2 (MAJOR)</option>
              <option value="error3">error3 (WARNING)</option>
            </select>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-600">故障表:</label>
            <select
              value={batchFaultMapId}
              onChange={(e) => setBatchFaultMapId(e.target.value)}
              className="neu-input px-2 py-1 text-xs bg-transparent min-w-[120px]"
            >
              <option value="__keep__">不变</option>
              <option value="__clear__">清除</option>
              {faultMaps.map((m) => (
                <option key={m.id} value={m.id}>{m.name}</option>
              ))}
            </select>
          </div>
          <button
            onClick={handleBatchApply}
            disabled={batchSaving}
            className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50"
          >
            {batchSaving ? '应用中...' : '批量应用'}
          </button>
          <button
            onClick={() => {
              setSelectedIds(new Set())
              setBatchScale('')
              setBatchOffset('')
              setBatchUnit('')
              setBatchReadWrite('')
              setBatchEnabled('')
              setBatchTargetNode('')
            }}
            className="neu-btn px-3 py-1.5 text-xs text-gray-500"
          >
            取消选择
          </button>
          <button
            onClick={handleBatchDelete}
            disabled={batchSaving}
            className="neu-btn px-3 py-1.5 text-xs font-medium text-white bg-red-500 hover:bg-red-600 disabled:opacity-50"
          >
            批量删除
          </button>
        </div>
      )}

      {/* 表格 */}
      <div className="neu-card overflow-hidden">
        <div className="table-container overflow-x-auto max-h-[600px] overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[#f0f0f0] z-10">
              <tr className="border-b border-gray-200">
                <th className="px-3 py-2 w-10">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    ref={(el) => { if (el) el.indeterminate = someSelected && !allSelected }}
                    onChange={toggleAll}
                    className="w-4 h-4 accent-[#52c41a]"
                  />
                </th>
                {SORTABLE_COLUMNS.map((col) => (
                  <th
                    key={col.key}
                    className={`px-3 py-2 font-medium text-gray-500 text-[11px] uppercase tracking-wider cursor-pointer select-none hover:text-gray-700 ${
                      ['raw_value', 'eng_value', 'scale_factor', 'value_offset'].includes(col.key) ? 'text-right' : 'text-left'
                    }`}
                    onClick={() => handleSort(col.key)}
                  >
                    <div className={`flex items-center gap-1 ${['raw_value', 'eng_value', 'scale_factor', 'value_offset'].includes(col.key) ? 'justify-end' : ''}`}>
                      {col.label}
                      {sortBy === col.key && (
                        <span className="text-[#52c41a]">{sortOrder === 'asc' ? '↑' : '↓'}</span>
                      )}
                    </div>
                  </th>
                ))}
                <th className="text-center px-3 py-2 font-medium text-gray-500 text-[11px] uppercase tracking-wider">公式</th>
                <th className="text-center px-3 py-2 font-medium text-gray-500 text-[11px] uppercase tracking-wider">操作</th>
              </tr>
            </thead>
            <tbody>
              {tags.map((tag) => {
                const rt = realtimeValues.get(tag.id)
                const rawVal = rt?.raw_value ?? tag.raw_value
                const engVal = rt?.eng_value ?? tag.eng_value

                return (
                  <tr
                    key={tag.id}
                    className={`border-b border-gray-100 hover:bg-white/30 ${selectedIds.has(tag.id) ? 'bg-[#52c41a]/5' : ''}`}
                  >
                    <td className="px-3 py-2">
                      <input
                        type="checkbox"
                        checked={selectedIds.has(tag.id)}
                        onChange={() => toggleOne(tag.id)}
                        className="w-4 h-4 accent-[#52c41a]"
                      />
                    </td>
                    <td className="px-3 py-2">
                      <button
                        onClick={() => setTrendTag(tag)}
                        className="text-left hover:text-[#389e0d] transition-colors"
                        title="点击查看趋势"
                      >
                        <div className="font-medium text-gray-800 whitespace-nowrap underline decoration-dotted underline-offset-2 decoration-gray-300 hover:decoration-[#52c41a]">
                          {tag.display_name || tag.name}
                          {tag.alarm_level && (
                            <span className={`ml-2 text-[10px] px-1 py-0.5 rounded border ${
                              tag.alarm_level === 'error1' ? 'bg-red-100 text-red-700 border-red-200' :
                              tag.alarm_level === 'error2' ? 'bg-orange-100 text-orange-700 border-orange-200' :
                              'bg-amber-100 text-amber-700 border-amber-200'
                            }`}>{tag.alarm_level}</span>
                          )}
                          {tag.alarm_type && (
                            <span className="ml-2 text-[10px] px-1 py-0.5 rounded bg-indigo-100 text-indigo-700 border border-indigo-200">
                              {tag.alarm_type}
                            </span>
                          )}
                          {tag.alarm_threshold != null && (
                            <span className="ml-2 text-[10px] px-1 py-0.5 rounded bg-amber-50 text-amber-600 border border-amber-100">
                              隈值:{tag.alarm_threshold}
                            </span>
                          )}
                          {tag.fault_map_name && (
                            <span className="ml-2 text-[10px] px-1 py-0.5 rounded bg-purple-100 text-purple-700 border border-purple-200">
                              {tag.fault_map_name}
                            </span>
                          )}
                        </div>
                        <div className="text-gray-400 text-[11px] whitespace-nowrap">{tag.name}</div>
                      </button>
                    </td>
                    <td className="px-3 py-2">
                      <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium ${
                        tag.data_type === 'FLOAT' ? 'bg-blue-100 text-blue-700' :
                        tag.data_type === 'INT' ? 'bg-purple-100 text-purple-700' :
                        tag.data_type === 'BOOL' ? 'bg-amber-100 text-amber-700' :
                        'bg-gray-100 text-gray-600'
                      }`}>{tag.data_type}</span>
                    </td>
                    <td className="px-3 py-2">
                      <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium ${
                        tag.tag_type === 'PHYSICAL' ? 'bg-emerald-100 text-emerald-700' : 'bg-indigo-100 text-indigo-700'
                      }`}>{tag.tag_type}</span>
                    </td>
                    <td className="px-3 py-2 text-gray-500">{tag.unit || '—'}</td>
                    <td className={`px-3 py-2 text-right font-mono-value ${rt ? 'value-flash' : ''}`}>
                      {rawVal !== null && rawVal !== undefined ? (
                        <span className="text-gray-700">{typeof rawVal === 'number' ? rawVal.toFixed(2) : String(rawVal)}</span>
                      ) : (
                        <span className="text-gray-300">—</span>
                      )}
                    </td>
                    <td className={`px-3 py-2 text-right font-mono-value ${rt ? 'value-flash' : ''}`}>
                      {engVal !== null && engVal !== undefined ? (
                        <span className="text-[#389e0d] font-medium">{typeof engVal === 'number' ? engVal.toFixed(4) : String(engVal)}</span>
                      ) : (
                        <span className="text-gray-300">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <EditableCell value={tag.scale_factor} onSave={(v) => handleUpdateScale(tag.id, v)} />
                    </td>
                    <td className="px-3 py-2 text-right">
                      <EditableCell value={tag.value_offset} onSave={(v) => handleUpdateOffset(tag.id, v)} />
                    </td>
                    <td className="px-3 py-2 text-center">
                      <span
                        className={`inline-block w-2 h-2 rounded-full mr-1 ${isOnline(tag) ? 'bg-green-500' : 'bg-gray-300'}`}
                        title={isOnline(tag) ? '质量良好' : `quality=${tag.quality ?? '—'}`}
                      />
                      <span className="text-[11px] text-gray-500">{tag.quality ?? '—'}</span>
                    </td>
                    <td className="px-3 py-2 text-[11px] text-gray-500">
                      {formatTs(tag.latest_ts)}
                    </td>
                    <td className="px-3 py-2 text-center text-[11px] text-gray-400 font-mono-value">
                      {!tag.enabled && <span className="text-red-500 mr-1">[已禁用]</span>}
                      {rawVal !== null ? (typeof rawVal === 'number' ? rawVal.toFixed(1) : '?') : '?'}
                      ×{tag.scale_factor}
                      {tag.value_offset >= 0 ? '+' : ''}{tag.value_offset}
                      ={engVal !== null ? (typeof engVal === 'number' ? engVal.toFixed(2) : '?') : '?'}
                    </td>
                    <td className="px-3 py-2 text-center">
                      <div className="flex items-center justify-center gap-2">
                        <button
                          onClick={() => setEditingTag(tag)}
                          className="text-[11px] text-[#389e0d] hover:underline"
                        >
                          编辑
                        </button>
                        <button
                          onClick={() => handleDeleteTag(tag.id)}
                          className="text-[11px] text-red-500 hover:underline"
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
              {tags.length === 0 && !loading && (
                <tr>
                  <td colSpan={12} className="px-3 py-8 text-center text-gray-400">
                    该节点下暂无点位
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {trendTag && (
        <TrendChart
          tagId={trendTag.id}
          tagName={trendTag.display_name || trendTag.name}
          unit={trendTag.unit}
          onClose={() => setTrendTag(null)}
        />
      )}

      {(editingTag || showCreateModal) && (
        <TagFormModal
          tag={editingTag}
          nodeId={nodeId}
          onClose={() => { setEditingTag(null); setShowCreateModal(false) }}
          onSave={handleSaveTag}
        />
      )}
    </div>
  )
}

function TagFormModal({
  tag,
  nodeId,
  onClose,
  onSave,
}: {
  tag: Tag | null
  nodeId: string
  onClose: () => void
  onSave: (form: Partial<TagCreateInput>) => void | Promise<void>
}) {
  const isEdit = !!tag
  const [name, setName] = useState(tag?.name || '')
  const [displayName, setDisplayName] = useState(tag?.display_name || '')
  const [dataType, setDataType] = useState(tag?.data_type || 'FLOAT')
  const [tagType, setTagType] = useState(tag?.tag_type || 'PHYSICAL')
  const [unit, setUnit] = useState(tag?.unit || '')
  const [readWrite, setReadWrite] = useState(tag?.read_write || 'R')
  const [description, setDescription] = useState(tag?.description || '')
  const [sourcePath, setSourcePath] = useState(tag?.source_path || '')
  const [saving, setSaving] = useState(false)

  // -- Virtual point (LOGICAL) formula config --
  const [formulaType, setFormulaType] = useState(tag?.formula_type || 'expression')
  const [aggregateFn, setAggregateFn] = useState(tag?.aggregate_fn || 'SUM')
  const [formula, setFormula] = useState(tag?.formula || '')
  const [sources, setSources] = useState<string[]>(tag?.sources || [])
  const [availableTags, setAvailableTags] = useState<Tag[]>([])
  const [sourceNodeFilter, setSourceNodeFilter] = useState(nodeId)
  const [allNodes, setAllNodes] = useState<Node[]>([])

  useEffect(() => {
    fetchTags(sourceNodeFilter, 1, 200, undefined, undefined, undefined, undefined, undefined).then((data) => {
      setAvailableTags(data.tags.filter((t) => t.id !== tag?.id))
    }).catch(() => {})
  }, [sourceNodeFilter, tag?.id])

  useEffect(() => {
    fetchNodes().then(setAllNodes).catch(() => {})
  }, [])

  const toggleSource = (id: string) => {
    setSources((prev) => {
      if (prev.includes(id)) return prev.filter((s) => s !== id)
      return [...prev, id]
    })
  }

  const sourceVar = (idx: number) => `s${idx}`

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) {
      alert('点位名不能为空')
      return
    }
    if (tagType === 'LOGICAL') {
      if (formulaType === 'aggregate' && sources.length === 0) {
        alert('聚合点位至少需要选择一个来源点位')
        return
      }
      if ((formulaType === 'expression' || formulaType === 'condition') && !formula.trim()) {
        alert('请填写公式表达式')
        return
      }
      if ((formulaType === 'expression' || formulaType === 'condition') && sources.length === 0) {
        alert('公式点位至少需要选择一个来源点位')
        return
      }
    }
    setSaving(true)
    const form: Partial<TagCreateInput> = {
      name: name.trim(),
      display_name: displayName.trim() || undefined,
      data_type: dataType,
      tag_type: tagType as 'PHYSICAL' | 'LOGICAL',
      unit: unit.trim() || undefined,
      description: description.trim() || undefined,
            read_write: readWrite,
            source_path: tagType === 'PHYSICAL' ? sourcePath.trim() || undefined : undefined,
            source_type: tagType === 'PHYSICAL' ? 'neuron' : 'manual',
          }
    if (tagType === 'LOGICAL') {
      form.formula_type = formulaType
      form.sources = sources
      if (formulaType === 'aggregate') {
        form.aggregate_fn = aggregateFn
      } else {
        form.formula = formula.trim()
      }
    }
    try {
      await onSave(form)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="neu-card w-[560px] max-w-[90vw] p-5 max-h-[90vh] overflow-y-auto">
        <h3 className="text-sm font-bold text-gray-800 mb-4">{isEdit ? '编辑点位' : '新建点位'}</h3>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-gray-600 mb-1">点位名 *</label>
              <input
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                disabled={isEdit}
                className="neu-input w-full px-3 py-1.5 text-xs disabled:bg-gray-100"
                placeholder="例如：PCS功率"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-600 mb-1">显示名</label>
              <input
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className="neu-input w-full px-3 py-1.5 text-xs"
                placeholder="例如：PCS 有功功率"
              />
            </div>
          </div>

          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="block text-xs text-gray-600 mb-1">数据类型</label>
              <select
                value={dataType}
                onChange={(e) => setDataType(e.target.value)}
                className="neu-input w-full px-3 py-1.5 text-xs bg-transparent"
              >
                <option value="FLOAT">FLOAT</option>
                <option value="INT">INT</option>
                <option value="BOOL">BOOL</option>
                <option value="STRING">STRING</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-600 mb-1">点位类型</label>
              <select
                value={tagType}
                onChange={(e) => setTagType(e.target.value)}
                className="neu-input w-full px-3 py-1.5 text-xs bg-transparent"
              >
                <option value="PHYSICAL">PHYSICAL（物理点位）</option>
                <option value="LOGICAL">LOGICAL（虚拟点位）</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-600 mb-1">读写</label>
              <select
                value={readWrite}
                onChange={(e) => setReadWrite(e.target.value)}
                className="neu-input w-full px-3 py-1.5 text-xs bg-transparent"
              >
                <option value="R">R</option>
                <option value="RW">RW</option>
                <option value="W">W</option>
              </select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-gray-600 mb-1">单位</label>
              <input
                value={unit}
                onChange={(e) => setUnit(e.target.value)}
                className="neu-input w-full px-3 py-1.5 text-xs"
                placeholder="kW"
              />
            </div>
            {tagType === 'PHYSICAL' && (
            <div>
              <label className="block text-xs text-gray-600 mb-1">Neuron 来源路径</label>
              <input
                value={sourcePath}
                onChange={(e) => setSourcePath(e.target.value)}
                className="neu-input w-full px-3 py-1.5 text-xs"
                placeholder="node/group/tag（或完整 neuron/node/group/tag）"
              />
            </div>
            )}
          </div>

          <div>
            <label className="block text-xs text-gray-600 mb-1">描述</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="neu-input w-full px-3 py-1.5 text-xs"
              rows={2}
              placeholder="点位用途说明"
            />
          </div>

          {/* Virtual point formula config */}
          {tagType === 'LOGICAL' && (
            <div className="border border-indigo-200 rounded-lg p-3 bg-indigo-50/30 space-y-3">
              <div className="flex items-center gap-2">
                <span className="text-xs font-bold text-indigo-700">虚拟点位配置</span>
                <span className="text-[10px] text-gray-400">（由后端聚合器/公式引擎每 10s 自动计算）</span>
              </div>

              <div>
                <label className="block text-xs text-gray-600 mb-1">计算方式</label>
                <select
                  value={formulaType}
                  onChange={(e) => setFormulaType(e.target.value)}
                  className="neu-input w-full px-3 py-1.5 text-xs bg-transparent"
                >
                  <option value="aggregate">聚合（SUM/AVG/MAX/MIN...）</option>
                  <option value="expression">表达式（s0 * 2 + s1）</option>
                  <option value="condition">条件判断（s0 &gt; 100 and s1 &lt; 50）</option>
                </select>
              </div>

              {formulaType === 'aggregate' && (
                <div>
                  <label className="block text-xs text-gray-600 mb-1">聚合函数</label>
                  <select
                    value={aggregateFn}
                    onChange={(e) => setAggregateFn(e.target.value)}
                    className="neu-input w-full px-3 py-1.5 text-xs bg-transparent"
                  >
                    <option value="SUM">SUM（求和）</option>
                    <option value="AVG">AVG（平均）</option>
                    <option value="MAX">MAX（最大值）</option>
                    <option value="MIN">MIN（最小值）</option>
                    <option value="COUNT">COUNT（计数）</option>
                    <option value="LAST">LAST（最新值）</option>
                  </select>
                </div>
              )}

              {(formulaType === 'expression' || formulaType === 'condition') && (
                <div>
                  <label className="block text-xs text-gray-600 mb-1">
                    {formulaType === 'condition' ? '条件表达式' : '计算公式'} *
                  </label>
                  <input
                    value={formula}
                    onChange={(e) => setFormula(e.target.value)}
                    className="neu-input w-full px-3 py-1.5 text-xs font-mono"
                    placeholder={formulaType === 'condition' ? 's0 > 100 and s1 < 50' : 's0 * 2 + s1'}
                  />
                  <p className="text-[10px] text-gray-400 mt-1">
                    用 s0, s1, s2... 引用下方选中的来源点位（按选择顺序编号）
                  </p>
                </div>
              )}

              {/* Source tags picker */}
              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="text-xs text-gray-600">来源点位 *（{sources.length} 个已选）</label>
                  <select
                    value={sourceNodeFilter}
                    onChange={(e) => setSourceNodeFilter(e.target.value)}
                    className="neu-input px-2 py-1 text-[11px] bg-transparent w-auto"
                  >
                    {allNodes.map((n) => (
                      <option key={n.id} value={n.id}>{n.name}</option>
                    ))}
                  </select>
                </div>
                <div className="border border-gray-200 rounded max-h-[180px] overflow-y-auto bg-white/50">
                  {availableTags.length === 0 && (
                    <div className="px-3 py-4 text-center text-[11px] text-gray-400">该节点下无可用点位</div>
                  )}
                  {availableTags.map((t) => {
                    const selIdx = sources.indexOf(t.id)
                    const isSelected = selIdx >= 0
                    return (
                      <label
                        key={t.id}
                        className="flex items-center gap-2 px-3 py-1.5 hover:bg-indigo-50 cursor-pointer border-b border-gray-50 last:border-0"
                      >
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => toggleSource(t.id)}
                          className="w-3.5 h-3.5 accent-indigo-500"
                        />
                        {isSelected && (
                          <span className="text-[10px] font-mono text-indigo-600 font-bold w-6">{sourceVar(selIdx)}</span>
                        )}
                        <span className="text-xs text-gray-700">{t.display_name || t.name}</span>
                        <span className="text-[10px] text-gray-400">{t.name}</span>
                        <span className="ml-auto text-[10px] text-gray-400">{t.data_type}</span>
                        <span className={`text-[10px] px-1 rounded ${t.tag_type === 'PHYSICAL' ? 'bg-emerald-100 text-emerald-600' : 'bg-indigo-100 text-indigo-600'}`}>{t.tag_type}</span>
                      </label>
                    )
                  })}
                </div>
                {sources.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {sources.map((sid, idx) => {
                      const t = availableTags.find((a) => a.id === sid)
                      return (
                        <span key={sid} className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700 font-mono">
                          {sourceVar(idx)}={t?.display_name || t?.name || sid.slice(0, 8)}
                        </span>
                      )
                    })}
                  </div>
                )}
              </div>
            </div>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <button type="button" onClick={onClose} className="neu-btn px-4 py-1.5 text-xs text-gray-600">
              取消
            </button>
            <button
              type="submit"
              disabled={saving}
              className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50"
            >
              {saving ? '保存中...' : '保存'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
