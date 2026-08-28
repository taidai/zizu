import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactECharts from 'echarts-for-react'

import { fetchTagHistory, fetchTags, type HistoryPoint, type Tag } from '../api/client'
import { RAW_HISTORY_INITIAL_SELECTION } from './data-trunk/dataTrunkViewModel'

type RangeOption = '1h' | '24h' | '7d'
type ViewMode = 'trend' | 'table'

const RANGE_OPTIONS: Array<{ key: RangeOption; label: string }> = [
  { key: '1h', label: '1小时' },
  { key: '24h', label: '24小时' },
  { key: '7d', label: '7天' },
]

export default function RawPointHistoryPanel({ nodeId }: { nodeId: string }) {
  const [tags, setTags] = useState<Tag[]>([])
  const [selectedTagId, setSelectedTagId] = useState<string | null>(RAW_HISTORY_INITIAL_SELECTION)
  const [range, setRange] = useState<RangeOption>('1h')
  const [viewMode, setViewMode] = useState<ViewMode>('trend')
  const [points, setPoints] = useState<HistoryPoint[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const activeNodeIdRef = useRef(nodeId)
  const tagGenerationRef = useRef(0)
  const historyGenerationRef = useRef(0)
  activeNodeIdRef.current = nodeId

  useEffect(() => () => {
    tagGenerationRef.current += 1
    historyGenerationRef.current += 1
    activeNodeIdRef.current = ''
  }, [])

  const loadTags = useCallback(async () => {
    const expectedNodeId = nodeId
    const generation = ++tagGenerationRef.current
    setError('')
    try {
      const data = await fetchTags(
        expectedNodeId, 1, 500, undefined, undefined, 'PHYSICAL',
        undefined, true, 'sort_order', 'asc',
      )
      if (generation !== tagGenerationRef.current
        || activeNodeIdRef.current !== expectedNodeId) return
      setTags(data.tags.filter((tag) => tag.data_type === 'FLOAT' || tag.data_type === 'INT'))
    } catch {
      if (generation === tagGenerationRef.current
        && activeNodeIdRef.current === expectedNodeId) {
        setTags([])
        setError('原始点位列表读取失败，请稍后重试')
      }
    }
  }, [nodeId])

  useEffect(() => {
    tagGenerationRef.current += 1
    historyGenerationRef.current += 1
    setSelectedTagId(RAW_HISTORY_INITIAL_SELECTION)
    setPoints([])
    setLoading(false)
    void loadTags()
  }, [loadTags])

  useEffect(() => {
    if (!selectedTagId) {
      setPoints([])
      setLoading(false)
      return
    }
    const expectedNodeId = nodeId
    const generation = ++historyGenerationRef.current
    setLoading(true)
    setError('')
    fetchTagHistory(selectedTagId, range)
      .then((response) => {
        if (generation === historyGenerationRef.current
          && activeNodeIdRef.current === expectedNodeId) setPoints(response.points)
      })
      .catch(() => {
        if (generation === historyGenerationRef.current
          && activeNodeIdRef.current === expectedNodeId) {
          setPoints([])
          setError('该点位的历史数据读取失败，请稍后重试')
        }
      })
      .finally(() => {
        if (generation === historyGenerationRef.current
          && activeNodeIdRef.current === expectedNodeId) setLoading(false)
      })
  }, [nodeId, range, selectedTagId])

  const selectedTag = tags.find((tag) => tag.id === selectedTagId) || null
  const chartOption = useMemo(() => ({
    backgroundColor: 'transparent',
    animation: false,
    grid: { left: 60, right: 24, top: 24, bottom: 36 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'time', axisLabel: { color: '#666', fontSize: 11 } },
    yAxis: {
      type: 'value',
      name: selectedTag?.unit || '',
      axisLabel: { color: '#666', fontSize: 11 },
      splitLine: { lineStyle: { color: '#e8ecf1', type: 'dashed' } },
    },
    series: [{
      name: selectedTag?.display_name || selectedTag?.name || '点位',
      type: 'line',
      showSymbol: false,
      smooth: true,
      lineStyle: { color: '#52c41a', width: 2 },
      data: points.map((point) => [point.ts, point.eng_value]),
    }],
  }), [points, selectedTag])

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <label className="min-w-64 text-xs font-medium text-gray-700">
          选择一个原始点位
          <select
            value={selectedTagId || ''}
            onChange={(event) => setSelectedTagId(event.target.value || null)}
            className="neu-input mt-1.5 w-full bg-transparent px-3 py-2 text-xs"
          >
            <option value="">请选择点位</option>
            {tags.map((tag) => (
              <option key={tag.id} value={tag.id}>
                {tag.display_name || tag.name}{tag.unit ? `（${tag.unit}）` : ''}
              </option>
            ))}
          </select>
        </label>
        <div className="flex flex-wrap gap-2">
          {RANGE_OPTIONS.map((option) => (
            <button key={option.key} type="button" onClick={() => setRange(option.key)} className={`rounded px-3 py-1.5 text-xs font-medium ${range === option.key ? 'bg-[#52c41a] text-white' : 'bg-gray-100 text-gray-600'}`}>
              {option.label}
            </button>
          ))}
          {([['trend', '趋势'], ['table', '明细']] as const).map(([key, label]) => (
            <button key={key} type="button" onClick={() => setViewMode(key)} className={`rounded px-3 py-1.5 text-xs font-medium ${viewMode === key ? 'bg-blue-700 text-white' : 'bg-gray-100 text-gray-600'}`}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {error && <div role="alert" className="rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>}
      {!selectedTagId && <div className="rounded border border-dashed border-gray-300 px-4 py-12 text-center text-sm text-gray-500">先选择一个原始点位，再读取它的历史数据。</div>}
      {selectedTagId && loading && <div className="px-4 py-12 text-center text-sm text-gray-500">正在读取历史数据...</div>}
      {selectedTagId && !loading && viewMode === 'trend' && (
        <div className="rounded-lg border border-gray-200 bg-white p-3"><ReactECharts option={chartOption} style={{ height: 360, width: '100%' }} notMerge /></div>
      )}
      {selectedTagId && !loading && viewMode === 'table' && (
        <div className="max-h-[560px] overflow-auto rounded-lg border border-gray-200 bg-white">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-gray-100 text-gray-600"><tr><th className="px-3 py-2 text-left">时间</th><th className="px-3 py-2 text-right">原始值</th><th className="px-3 py-2 text-right">工程值</th></tr></thead>
            <tbody>
              {points.map((point) => <tr key={point.ts} className="border-t border-gray-100"><td className="px-3 py-2">{new Date(point.ts).toLocaleString('zh-CN')}</td><td className="px-3 py-2 text-right font-mono-value">{point.raw_value ?? '无'}</td><td className="px-3 py-2 text-right font-mono-value">{point.eng_value ?? '无'}</td></tr>)}
              {points.length === 0 && <tr><td colSpan={3} className="px-3 py-10 text-center text-gray-400">该时段没有历史数据</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
