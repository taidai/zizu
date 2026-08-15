import React, { useEffect, useState, useCallback, useRef } from 'react'
import {
  fetchNodes, fetchTags, fetchTelemetry,
  exportTelemetryCsv,
  type Node, type Tag, type TelemetryPoint,
} from '../api/client'

type TimeRange = '1h' | '24h' | '7d' | 'all'

const PAGE_SIZE = 20

export default function DataBrowser() {
  const [nodes, setNodes] = useState<Node[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [selectedNode, setSelectedNode] = useState('')
  const [selectedTag, setSelectedTag] = useState('')
  const [range, setRange] = useState<TimeRange>('1h')

  const [rows, setRows] = useState<TelemetryPoint[]>([])
  const [page, setPage] = useState(1)
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(false)
  const pageCursor = cursors[page - 1] || null
  const requestSequence = useRef(0)

  useEffect(() => {
    fetchNodes().then(setNodes).catch(() => {})
  }, [])

  useEffect(() => {
    setSelectedTag('')
    setTags([])
    if (!selectedNode) return
    fetchTags(selectedNode, 1, 500)
      .then((r) => setTags(r.tags))
      .catch(() => {})
  }, [selectedNode])

  const loadData = useCallback(async () => {
    const requestId = ++requestSequence.current
    setLoading(true)
    try {
      const r = await fetchTelemetry(
        selectedTag || undefined,
        range,
        pageCursor,
        PAGE_SIZE,
        selectedNode || undefined,
      )
      if (requestId !== requestSequence.current) return
      setRows(r.points)
      setHasMore(r.has_more)
      setCursors((current) => {
        const next = current.slice(0, page)
        if (r.has_more && r.next_cursor) next[page] = r.next_cursor
        return next
      })
    } catch {
      if (requestId !== requestSequence.current) return
      setRows([])
      setHasMore(false)
    } finally {
      if (requestId === requestSequence.current) setLoading(false)
    }
  }, [selectedNode, selectedTag, range, page, pageCursor])

  useEffect(() => {
    void loadData()
  }, [loadData])

  const handleExport = () => {
    exportTelemetryCsv(selectedTag || undefined, range, selectedNode || undefined)
  }

  return (
    <div className="neu-card p-4">
      <h3 className="text-sm font-bold text-gray-800 mb-3">级联数据查询</h3>

      <div className="flex flex-wrap items-center gap-2 mb-3">
        <span className="text-xs text-gray-600">节点:</span>
        <select
          value={selectedNode}
          onChange={(e) => { setSelectedNode(e.target.value); setPage(1); setCursors([null]) }}
          className="neu-input px-2 py-1 text-xs bg-transparent min-w-[120px]"
        >
          <option value="">全部节点</option>
          {nodes.map((n) => (
            <option key={n.id} value={n.id}>
              {n.name} ({n.tag_count})
            </option>
          ))}
        </select>

        <span className="text-xs text-gray-400">→</span>
        <span className="text-xs text-gray-600">点位:</span>
        <select
          value={selectedTag}
          onChange={(e) => { setSelectedTag(e.target.value); setPage(1); setCursors([null]) }}
          disabled={!selectedNode}
          className="neu-input px-2 py-1 text-xs bg-transparent min-w-[140px] disabled:opacity-50"
        >
          <option value="">全部点位</option>
          {tags.map((t) => (
            <option key={t.id} value={t.id}>
              {t.display_name || t.name}
            </option>
          ))}
        </select>

        <span className="text-xs text-gray-400">→</span>
        <span className="text-xs text-gray-600">时间:</span>
        <select
          value={range}
          onChange={(e) => { setRange(e.target.value as TimeRange); setPage(1); setCursors([null]) }}
          className="neu-input px-2 py-1 text-xs bg-transparent"
        >
          <option value="1h">最近 1 小时</option>
          <option value="24h">最近 24 小时</option>
          <option value="7d">最近 7 天</option>
          <option value="all">全部</option>
        </select>

        <button
          onClick={loadData}
          disabled={loading}
          className="neu-btn px-3 py-1 text-xs font-medium text-gray-600 hover:text-[#389e0d] disabled:opacity-50"
        >
          {loading ? '查询中...' : '↻ 刷新'}
        </button>
        <button
          onClick={handleExport}
          className="neu-btn px-3 py-1 text-xs font-medium text-gray-600 hover:text-[#389e0d]"
        >
          导出 CSV
        </button>
      </div>

      <div className="neu-inset p-2 rounded-xl">
        <div className="overflow-x-auto max-h-[360px] overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[#f0f0f0]">
              <tr className="border-b border-gray-200">
                <th className="text-left px-2 py-1.5 font-medium text-gray-500 text-[11px] uppercase tracking-wider">时间</th>
                <th className="text-left px-2 py-1.5 font-medium text-gray-500 text-[11px] uppercase tracking-wider">节点</th>
                <th className="text-left px-2 py-1.5 font-medium text-gray-500 text-[11px] uppercase tracking-wider">点位</th>
                <th className="text-right px-2 py-1.5 font-medium text-gray-500 text-[11px] uppercase tracking-wider">原始值</th>
                <th className="text-right px-2 py-1.5 font-medium text-gray-500 text-[11px] uppercase tracking-wider">工程值</th>
                <th className="text-center px-2 py-1.5 font-medium text-gray-500 text-[11px] uppercase tracking-wider">Quality</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={6} className="text-center py-6 text-gray-400 text-xs">
                    {loading ? '查询中...' : '暂无数据'}
                  </td>
                </tr>
              ) : (
                rows.map((p, i) => (
                  <tr key={`${p.tag_id}-${p.ts}-${i}`} className="border-b border-gray-100 hover:bg-white/30">
                    <td className="px-2 py-1.5 text-gray-700 font-mono text-[11px]">
                      {new Date(p.ts).toLocaleString('zh-CN', { hour12: false })}
                    </td>
                    <td className="px-2 py-1.5 text-gray-700">{p.node_name}</td>
                    <td className="px-2 py-1.5 text-gray-700">{p.tag_name}</td>
                    <td className="px-2 py-1.5 text-right text-gray-700 font-mono">
                      {p.raw_value !== null ? p.raw_value.toFixed(2) : '—'}
                    </td>
                    <td className="px-2 py-1.5 text-right text-[#389e0d] font-mono font-medium">
                      {p.eng_value !== null ? p.eng_value.toFixed(2) : '—'}
                    </td>
                    <td className="px-2 py-1.5 text-center">
                      <span
                        className={`inline-block px-1.5 py-0.5 rounded-full text-[10px] font-medium ${
                          p.quality === 192
                            ? 'bg-green-100 text-green-700'
                            : 'bg-yellow-100 text-yellow-700'
                        }`}
                      >
                        {p.quality ?? '—'}
                      </span>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="flex items-center justify-between mt-2 text-xs text-gray-500">
        <span>
          第 {page} 页 · 本页 {rows.length} 条
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1 || loading}
            className="neu-btn px-3 py-1 text-xs font-medium text-gray-600 hover:text-[#389e0d] disabled:opacity-40"
          >
            ← 上一页
          </button>
          <button
            onClick={() => setPage((p) => p + 1)}
            disabled={!hasMore || loading}
            className="neu-btn px-3 py-1 text-xs font-medium text-gray-600 hover:text-[#389e0d] disabled:opacity-40"
          >
            下一页 →
          </button>
        </div>
      </div>
    </div>
  )
}
