/**
 * NodeHistoryPanel — 历史数据面板
 * 显示选中节点下点位的趋势图，支持多点位对比和时间范围选择。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactECharts from 'echarts-for-react'
import { fetchTags, fetchTagHistory, fetchTelemetry, type Tag, type HistoryPoint, type TelemetryPoint } from '../api/client'

interface NodeHistoryPanelProps {
  nodeId: string
}

type RangeOption = '1h' | '24h' | '7d'
type ViewMode = 'trend' | 'table'

const RANGE_OPTIONS: { key: RangeOption; label: string }[] = [
  { key: '1h', label: '1小时' },
  { key: '24h', label: '24小时' },
  { key: '7d', label: '7天' },
]

const PAGE_SIZE = 50

// Distinct colors for multi-series chart
const SERIES_COLORS = [
  '#52c41a', '#1890ff', '#fa8c16', '#eb2f96',
  '#722ed1', '#13c2c2', '#faad14', '#f5222d',
]

export default function NodeHistoryPanel({ nodeId }: NodeHistoryPanelProps) {
  const [tags, setTags] = useState<Tag[]>([])
  const [selectedTagIds, setSelectedTagIds] = useState<Set<string>>(new Set())
  const [range, setRange] = useState<RangeOption>('1h')
  const [viewMode, setViewMode] = useState<ViewMode>('trend')
  const [loading, setLoading] = useState(false)
  const [historyData, setHistoryData] = useState<Map<string, HistoryPoint[]>>(new Map())
  const [telemetryPoints, setTelemetryPoints] = useState<TelemetryPoint[]>([])
  const [telemetryPage, setTelemetryPage] = useState(1)
  const [telemetryCursors, setTelemetryCursors] = useState<(string | null)[]>([null])
  const [telemetryHasMore, setTelemetryHasMore] = useState(false)
  const [telemetryLoading, setTelemetryLoading] = useState(false)
  const chartRef = useRef<ReactECharts>(null)
  const telemetryCursor = telemetryCursors[telemetryPage - 1] || null
  const telemetryRequestSequence = useRef(0)

  const loadTags = useCallback(async () => {
    try {
      const data = await fetchTags(nodeId, 1, 200, undefined, undefined, undefined, undefined, true)
      // Only show tags that have numeric data (FLOAT/INT)
      const numericTags = data.tags.filter((t) => t.data_type === 'FLOAT' || t.data_type === 'INT')
      setTags(numericTags)
      // Auto-select first 3 tags
      if (numericTags.length > 0 && selectedTagIds.size === 0) {
        setSelectedTagIds(new Set(numericTags.slice(0, 3).map((t) => t.id)))
      }
    } catch {
      setTags([])
    }
  }, [nodeId])

  useEffect(() => {
    loadTags()
    // Reset selection when node changes
    setSelectedTagIds(new Set())
  }, [nodeId])

  const toggleTag = (id: string) => {
    setSelectedTagIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        if (next.size >= 8) return prev // Max 8 series
        next.add(id)
      }
      return next
    })
  }

  // Fetch history for all selected tags when range or selection changes
  useEffect(() => {
    if (selectedTagIds.size === 0) {
      setHistoryData(new Map())
      return
    }

    setLoading(true)
    const promises = Array.from(selectedTagIds).map(async (tagId) => {
      try {
        const data = await fetchTagHistory(tagId, range)
        return { tagId, points: data.points }
      } catch {
        return { tagId, points: [] }
      }
    })

    Promise.all(promises).then((results) => {
      const map = new Map<string, HistoryPoint[]>()
      for (const r of results) {
        map.set(r.tagId, r.points)
      }
      setHistoryData(map)
      setLoading(false)
    })
  }, [selectedTagIds, range])

  // Load raw telemetry records from DB for table view
  const loadTelemetry = useCallback(async () => {
    const requestId = ++telemetryRequestSequence.current
    setTelemetryLoading(true)
    try {
      const data = await fetchTelemetry(
        undefined,
        range,
        telemetryCursor,
        PAGE_SIZE,
        nodeId,
      )
      if (requestId !== telemetryRequestSequence.current) return
      setTelemetryPoints(data.points || [])
      setTelemetryHasMore(data.has_more)
      setTelemetryCursors((current) => {
        const next = current.slice(0, telemetryPage)
        if (data.has_more && data.next_cursor) next[telemetryPage] = data.next_cursor
        return next
      })
    } catch (err) {
      if (requestId !== telemetryRequestSequence.current) return
      console.error('[NodeHistoryPanel] loadTelemetry failed:', err)
      setTelemetryPoints([])
      setTelemetryHasMore(false)
    } finally {
      if (requestId === telemetryRequestSequence.current) setTelemetryLoading(false)
    }
  }, [nodeId, range, telemetryPage, telemetryCursor])

  useEffect(() => {
    if (viewMode === 'table') {
      loadTelemetry()
    }
  }, [viewMode, loadTelemetry])

  useEffect(() => {
    setTelemetryPage(1)
    setTelemetryCursors([null])
  }, [nodeId, range, viewMode])

  const buildChartOption = () => {
    const selectedTags = tags.filter((t) => selectedTagIds.has(t.id))
    const series = selectedTags.map((tag, idx) => {
      const points = historyData.get(tag.id) || []
      return {
        name: tag.display_name || tag.name,
        type: 'line' as const,
        showSymbol: false,
        smooth: true,
        lineStyle: { color: SERIES_COLORS[idx % SERIES_COLORS.length], width: 2 },
        areaStyle: idx === 0 ? {
          color: {
            type: 'linear' as const,
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: SERIES_COLORS[idx % SERIES_COLORS.length] + '30' },
              { offset: 1, color: SERIES_COLORS[idx % SERIES_COLORS.length] + '02' },
            ],
          },
        } : undefined,
        data: points.map((p) => [p.ts, p.eng_value]),
      }
    })

    return {
      backgroundColor: 'transparent',
      animation: false,
      grid: { left: 60, right: 30, top: 50, bottom: 30 },
      legend: {
        show: selectedTags.length > 1,
        top: 5,
        textStyle: { fontSize: 11, color: '#666' },
        data: selectedTags.map((t) => t.display_name || t.name),
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(255,255,255,0.95)',
        borderColor: '#d1d9e6',
        textStyle: { color: '#333', fontSize: 12 },
      },
      xAxis: {
        type: 'time',
        axisLine: { lineStyle: { color: '#d1d9e6' } },
        axisLabel: { color: '#666', fontSize: 11 },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        axisLine: { show: false },
        axisLabel: { color: '#666', fontSize: 11 },
        splitLine: { lineStyle: { color: '#e8ecf1', type: 'dashed' } },
      },
      series,
    }
  }

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-1">
          {RANGE_OPTIONS.map((opt) => (
            <button
              key={opt.key}
              onClick={() => { setRange(opt.key); setTelemetryPage(1); setTelemetryCursors([null]) }}
              className={`px-3 py-1.5 text-xs font-medium rounded-full transition-colors ${
                range === opt.key
                  ? 'bg-[#52c41a] text-white'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => { setViewMode('trend'); setTelemetryPage(1); setTelemetryCursors([null]) }}
            className={`px-3 py-1.5 text-xs font-medium rounded-full transition-colors ${
              viewMode === 'trend' ? 'bg-[#52c41a] text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
            }`}
          >
            趋势图
          </button>
          <button
            onClick={() => { setViewMode('table'); setTelemetryPage(1); setTelemetryCursors([null]) }}
            className={`px-3 py-1.5 text-xs font-medium rounded-full transition-colors ${
              viewMode === 'table' ? 'bg-[#52c41a] text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
            }`}
          >
            入库数据
          </button>
        </div>
      </div>

      {/* Tag selector */}
      <div className="neu-card p-3">
        <div className="flex flex-wrap gap-2">
          {tags.map((tag) => {
            const isSelected = selectedTagIds.has(tag.id)
            const tagIdx = tags.filter((t) => selectedTagIds.has(t.id)).indexOf(tag)
            return (
              <button
                key={tag.id}
                onClick={() => toggleTag(tag.id)}
                className={`px-2.5 py-1 text-xs rounded-full border transition-all ${
                  isSelected
                    ? 'text-white border-transparent'
                    : 'bg-gray-50 text-gray-500 border-gray-200 hover:border-gray-300'
                }`}
                style={isSelected ? { backgroundColor: SERIES_COLORS[tagIdx >= 0 ? tagIdx % SERIES_COLORS.length : 0] } : {}}
              >
                {tag.display_name || tag.name}
                {tag.unit ? ` (${tag.unit})` : ''}
              </button>
            )
          })}
          {tags.length === 0 && (
            <span className="text-xs text-gray-400 py-2">该节点下无数值型点位</span>
          )}
        </div>
      </div>

      {/* Trend Chart */}
      {viewMode === 'trend' && (
        selectedTagIds.size > 0 ? (
          <div className="neu-card p-4">
            {loading ? (
              <div className="h-[400px] flex items-center justify-center text-gray-400 text-sm">
                加载中...
              </div>
            ) : (
              <ReactECharts
                ref={chartRef}
                option={buildChartOption()}
                style={{ height: '400px', width: '100%' }}
                notMerge
              />
            )}
          </div>
        ) : (
          <div className="neu-card p-8 text-center text-gray-400 text-sm">
            请选择至少一个点位查看历史趋势
          </div>
        )
      )}

      {/* Raw telemetry table */}
      {viewMode === 'table' && (
        <div className="neu-card overflow-hidden">
          <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
            <span className="text-xs text-gray-500">
              第 {telemetryPage} 页 · 本页 {telemetryPoints.length} 条入库记录
            </span>
            <div className="flex items-center gap-1">
              <button
                onClick={() => setTelemetryPage((p) => Math.max(1, p - 1))}
                disabled={telemetryPage <= 1 || telemetryLoading}
                className="neu-btn w-7 h-7 flex items-center justify-center disabled:opacity-30"
              >
                ‹
              </button>
              <span className="px-2 font-mono text-xs">
                {telemetryPage}
              </span>
              <button
                onClick={() => setTelemetryPage((p) => p + 1)}
                disabled={!telemetryHasMore || telemetryLoading}
                className="neu-btn w-7 h-7 flex items-center justify-center disabled:opacity-30"
              >
                ›
              </button>
            </div>
          </div>
          <div className="table-container overflow-x-auto max-h-[600px] overflow-y-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-[#f0f0f0] z-10">
                <tr className="border-b border-gray-200">
                  <th className="px-3 py-2 text-left font-medium text-gray-500 text-[11px] uppercase tracking-wider">时间</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-500 text-[11px] uppercase tracking-wider">点位</th>
                  <th className="px-3 py-2 text-right font-medium text-gray-500 text-[11px] uppercase tracking-wider">原始值</th>
                  <th className="px-3 py-2 text-right font-medium text-gray-500 text-[11px] uppercase tracking-wider">工程值</th>
                  <th className="px-3 py-2 text-center font-medium text-gray-500 text-[11px] uppercase tracking-wider">质量</th>
                </tr>
              </thead>
              <tbody>
                {telemetryLoading ? (
                  <tr>
                    <td colSpan={5} className="px-3 py-8 text-center text-gray-400">加载中...</td>
                  </tr>
                ) : telemetryPoints.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="px-3 py-8 text-center text-gray-400">该时间范围内无入库记录</td>
                  </tr>
                ) : (
                  telemetryPoints.map((p, idx) => (
                    <tr key={idx} className="border-b border-gray-100 hover:bg-white/30">
                      <td className="px-3 py-2 text-[11px] text-gray-600 font-mono">{new Date(p.ts).toLocaleString('zh-CN')}</td>
                      <td className="px-3 py-2 text-gray-700">{p.tag_name}</td>
                      <td className="px-3 py-2 text-right font-mono-value text-gray-700">{p.raw_value !== null ? p.raw_value.toFixed(4) : '—'}</td>
                      <td className="px-3 py-2 text-right font-mono-value text-[#389e0d]">{p.eng_value !== null ? p.eng_value.toFixed(4) : '—'}</td>
                      <td className="px-3 py-2 text-center text-[11px] text-gray-500">{p.quality ?? '—'}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
