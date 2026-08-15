import { useEffect, useMemo, useState, useCallback, useRef } from 'react'
import {
  fetchNodes, fetchTags, fetchTelemetry, exportTelemetryCsv,
  type Node, type Tag, type TelemetryPoint,
} from '../api/client'

const formatNum = (v: number | null, digits = 4) =>
  v !== null ? v.toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: digits }) : '—'

export default function TelemetryTable() {
  const [nodes, setNodes] = useState<Node[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [selectedNode, setSelectedNode] = useState('')
  const [selectedTag, setSelectedTag] = useState('')
  const [range, setRange] = useState<'1h' | '24h' | '7d' | 'all'>('1h')
  const [points, setPoints] = useState<TelemetryPoint[]>([])
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const [hasMore, setHasMore] = useState(false)
  const pageSize = 50
  const pageCursor = cursors[page - 1] || null
  const requestSequence = useRef(0)

  useEffect(() => {
    fetchNodes().then((n) => setNodes(n.filter((node) => node.layer >= 3)))
  }, [])

  useEffect(() => {
    if (!selectedNode) {
      setTags([])
      setSelectedTag('')
      return
    }
    fetchTags(selectedNode, 1, 200).then((data) => setTags(data.tags))
  }, [selectedNode])

  const loadData = useCallback(async () => {
    const requestId = ++requestSequence.current
    setLoading(true)
    try {
      const data = await fetchTelemetry(
        selectedTag || undefined,
        range,
        pageCursor,
        pageSize,
      )
      // 按 tag_id + ts 去重，保留第一条
      const seen = new Set<string>()
      const deduped = data.points.filter((p) => {
        const key = `${p.tag_id}|${p.ts}`
        if (seen.has(key)) return false
        seen.add(key)
        return true
      })
      if (requestId !== requestSequence.current) return
      setPoints(deduped)
      setHasMore(data.has_more)
      setCursors((current) => {
        const next = current.slice(0, page)
        if (data.has_more && data.next_cursor) next[page] = data.next_cursor
        return next
      })
    } finally {
      if (requestId === requestSequence.current) setLoading(false)
    }
  }, [selectedTag, range, page, pageCursor])

  useEffect(() => { loadData() }, [loadData])

  useEffect(() => {
    setSelectedTag('')
    setPage(1)
    setCursors([null])
  }, [selectedNode])

  const selectedTagUnit = useMemo(() => {
    return tags.find((t) => t.id === selectedTag)?.unit || ''
  }, [tags, selectedTag])

  return (
    <div>
      {/* 工具栏 */}
      <div className="neu-card p-4 mb-4 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <label className="text-xs font-medium text-gray-600 whitespace-nowrap">节点:</label>
          <select
            value={selectedNode}
            onChange={(e) => setSelectedNode(e.target.value)}
            className="neu-input px-3 py-1.5 text-xs bg-transparent min-w-[140px]"
          >
            <option value="">全部节点</option>
            {nodes.map((n) => (
              <option key={n.id} value={n.id}>{n.name} ({n.tag_count})</option>
            ))}
          </select>
        </div>

        <div className="flex items-center gap-2">
          <label className="text-xs font-medium text-gray-600 whitespace-nowrap">点位:</label>
          <select
            value={selectedTag}
            onChange={(e) => { setSelectedTag(e.target.value); setPage(1); setCursors([null]) }}
            className="neu-input px-3 py-1.5 text-xs bg-transparent min-w-[160px]"
            disabled={!selectedNode}
          >
            <option value="">全部点位</option>
            {tags.map((t) => (
              <option key={t.id} value={t.id}>{t.display_name || t.name}{t.unit ? ` (${t.unit})` : ''}</option>
            ))}
          </select>
        </div>

        <div className="flex items-center gap-2">
          <label className="text-xs font-medium text-gray-600 whitespace-nowrap">时间:</label>
          {(['1h', '24h', '7d', 'all'] as const).map((r) => (
            <button
              key={r}
              onClick={() => { setRange(r); setPage(1); setCursors([null]) }}
              className={`px-3 py-1 text-xs rounded-full font-medium transition-colors ${
                range === r ? 'bg-[#52c41a] text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              {r === '1h' ? '1小时' : r === '24h' ? '24小时' : r === '7d' ? '7天' : '全部'}
            </button>
          ))}
        </div>

        <button
          onClick={loadData}
          disabled={loading}
          className="neu-btn px-4 py-1.5 text-xs font-medium text-[#389e0d] disabled:opacity-50"
        >
          {loading ? '加载中...' : '刷新'}
        </button>

        <button
          onClick={() => exportTelemetryCsv(selectedTag || undefined, range)}
          className="neu-btn px-4 py-1.5 text-xs font-medium text-gray-600 hover:text-[#389e0d]"
        >
          导出 CSV
        </button>

        <div className="ml-auto flex items-center gap-3 text-xs text-gray-500">
          <span>第 {page} 页 · 本页 {points.length} 条</span>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1 || loading}
              className="neu-btn w-7 h-7 flex items-center justify-center disabled:opacity-30"
            >
              ‹
            </button>
            <span className="px-2 font-mono">{page}</span>
            <button
              onClick={() => setPage((p) => p + 1)}
              disabled={!hasMore || loading}
              className="neu-btn w-7 h-7 flex items-center justify-center disabled:opacity-30"
            >
              ›
            </button>
          </div>
        </div>
      </div>

      {/* 数据表 */}
      <div className="neu-card overflow-hidden">
        <div className="table-container overflow-x-auto max-h-[600px] overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[#f0f0f0] z-10">
              <tr className="border-b border-gray-200">
                <th className="text-left px-3 py-2 font-medium text-gray-500 text-[11px] uppercase tracking-wider">时间</th>
                <th className="text-left px-3 py-2 font-medium text-gray-500 text-[11px] uppercase tracking-wider">节点</th>
                <th className="text-left px-3 py-2 font-medium text-gray-500 text-[11px] uppercase tracking-wider">点位</th>
                <th className="text-right px-3 py-2 font-medium text-gray-500 text-[11px] uppercase tracking-wider">原始值</th>
                <th className="text-right px-3 py-2 font-medium text-gray-500 text-[11px] uppercase tracking-wider">工程值 {selectedTagUnit ? `(${selectedTagUnit})` : ''}</th>
                <th className="text-center px-3 py-2 font-medium text-gray-500 text-[11px] uppercase tracking-wider">Quality</th>
              </tr>
            </thead>
            <tbody>
              {points.map((p) => (
                <tr key={`${p.tag_id}-${p.ts}`} className="border-b border-gray-100 hover:bg-white/30">
                  <td className="px-3 py-2 text-gray-600 font-mono text-[11px]">
                    {new Date(p.ts).toLocaleString('zh-CN', { hour12: false })}
                  </td>
                  <td className="px-3 py-2 text-gray-600">{p.node_name}</td>
                  <td className="px-3 py-2 text-gray-800 font-medium">{p.tag_name}</td>
                  <td className="px-3 py-2 text-right font-mono-value">
                    {formatNum(p.raw_value)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono-value text-[#389e0d]">
                    {formatNum(p.eng_value)}
                  </td>
                  <td className="px-3 py-2 text-center">
                    <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium ${
                      p.quality === 192 ? 'bg-green-100 text-green-700' :
                      p.quality === 0 ? 'bg-gray-100 text-gray-600' :
                      'bg-amber-100 text-amber-700'
                    }`}>
                      {p.quality === 192 ? 'GOOD' : p.quality === 0 ? 'BAD' : p.quality ?? '—'}
                    </span>
                  </td>
                </tr>
              ))}
              {points.length === 0 && !loading && (
                <tr>
                  <td colSpan={6} className="px-3 py-8 text-center text-gray-400">
                    暂无数据
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
