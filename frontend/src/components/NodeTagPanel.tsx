import { useCallback, useEffect, useState } from 'react'
import { fetchTags, type Tag } from '../api/client'
import {
  connectCommittedFrameStream,
  fetchCommittedFrameSnapshot,
} from '../api/committedFrameStream'
import {
  applyFrameDelta,
  replaceSnapshot,
  type CommittedFrameProjection,
} from './data-trunk/committedFrameProjection'
import { qualityLabel, RAW_POINT_COLUMNS } from './data-trunk/dataTrunkViewModel'
import NodeHistoryPanel from './NodeHistoryPanel'

interface NodeTagPanelProps {
  nodeId: string
}

type RawPointView = 'realtime' | 'history'

const PAGE_SIZE = 50

function formatTime(timestamp: string | null | undefined): string {
  return timestamp ? new Date(timestamp).toLocaleString('zh-CN') : '未收到'
}

export default function NodeTagPanel({ nodeId }: NodeTagPanelProps) {
  const [view, setView] = useState<RawPointView>('realtime')
  const [tags, setTags] = useState<Tag[]>([])
  const [projection, setProjection] = useState<CommittedFrameProjection | null>(null)
  const [search, setSearch] = useState('')
  const [dataType, setDataType] = useState('')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(1)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const loadTags = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await fetchTags(
        nodeId,
        page,
        PAGE_SIZE,
        search || undefined,
        dataType || undefined,
        'PHYSICAL',
        undefined,
        true,
        'sort_order',
        'asc',
      )
      setTags(data.tags)
      setTotal(data.total)
      setTotalPages(data.total_pages || 1)
    } catch {
      setTags([])
      setError('原始点位读取失败，请稍后重试')
    } finally {
      setLoading(false)
    }
  }, [dataType, nodeId, page, search])

  useEffect(() => {
    setPage(1)
  }, [nodeId, search, dataType])

  useEffect(() => {
    void loadTags()
  }, [loadTags])

  useEffect(() => {
    let active = true
    let generation = 0
    let stopStream = () => {}
    let controller: AbortController | null = null

    const start = async () => {
      const currentGeneration = ++generation
      stopStream()
      controller?.abort()
      controller = new AbortController()
      setProjection(null)
      try {
        const snapshot = await fetchCommittedFrameSnapshot(nodeId, controller.signal)
        if (!active || currentGeneration !== generation) return
        setProjection(replaceSnapshot(null, snapshot))
        stopStream = connectCommittedFrameStream({
          nodeId,
          cursor: snapshot.cursor,
          onDelta: (delta) => {
            if (!active || currentGeneration !== generation) return
            setProjection((current) => {
              if (!current || current.nodeId !== nodeId) return current
              try {
                return applyFrameDelta(current, delta)
              } catch {
                void start()
                return current
              }
            })
          },
          onResnapshotRequired: () => { void start() },
        })
      } catch (reason) {
        if (active && currentGeneration === generation
          && !(reason instanceof DOMException && reason.name === 'AbortError')) {
          setProjection(null)
        }
      }
    }

    void start()
    return () => {
      active = false
      generation += 1
      controller?.abort()
      stopStream()
    }
  }, [nodeId])

  return (
    <section className="neu-card min-h-full p-4" aria-label="原始点位">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-bold text-gray-800">原始点位</h3>
          <p className="mt-1 text-xs text-gray-500">查看设备实际上传的值。点位配置请使用节点上方的“导入点位”。</p>
        </div>
        <div className="flex gap-2" aria-label="原始点位数据视图">
          {([
            ['realtime', '实时'],
            ['history', '历史'],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setView(key)}
              className={`rounded px-4 py-1.5 text-xs font-medium ${
                view === key
                  ? 'bg-[#52c41a] text-white'
                  : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {view === 'history' ? (
        <NodeHistoryPanel nodeId={nodeId} />
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <label className="text-xs text-gray-600">
              <span className="sr-only">搜索点位</span>
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="搜索点位名称"
                className="neu-inset w-56 rounded px-3 py-2 text-xs text-gray-800 outline-none focus:ring-2 focus:ring-[#52c41a]/30"
              />
            </label>
            <label className="text-xs text-gray-600">
              <span className="sr-only">数据类型</span>
              <select
                value={dataType}
                onChange={(event) => setDataType(event.target.value)}
                className="neu-inset rounded px-3 py-2 text-xs text-gray-700 outline-none focus:ring-2 focus:ring-[#52c41a]/30"
              >
                <option value="">全部类型</option>
                <option value="FLOAT">浮点数</option>
                <option value="INT">整数</option>
                <option value="BOOL">布尔值</option>
                <option value="STRING">字符串</option>
              </select>
            </label>
            <span className="ml-auto text-xs text-gray-500">共 {total} 个点位</span>
          </div>

          {error && (
            <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </div>
          )}

          <div className="overflow-x-auto rounded border border-gray-200 bg-white">
            <table className="w-full text-left text-xs">
              <thead className="bg-gray-50 text-gray-500">
                <tr>
                  {RAW_POINT_COLUMNS.map((label) => (
                    <th key={label} className="whitespace-nowrap px-3 py-2 font-medium">{label}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {!loading && tags.map((tag) => {
                  const current = projection?.l0.get(tag.id)
                  const value = current?.value
                  return (
                    <tr key={tag.id} className="text-gray-700 hover:bg-gray-50">
                      <td className="px-3 py-2 font-medium text-gray-800">{tag.display_name || tag.name}</td>
                      <td className="px-3 py-2 font-mono">{value === null || value === undefined ? '-' : String(value)}</td>
                      <td className="px-3 py-2">{current?.unit || tag.unit || '-'}</td>
                      <td className="px-3 py-2">{qualityLabel(current?.effective_quality ?? 1)}</td>
                      <td className="whitespace-nowrap px-3 py-2">{formatTime(current?.source_timestamp)}</td>
                      <td className="max-w-80 truncate px-3 py-2 font-mono text-[11px] text-gray-500" title={current?.source_path || tag.source_path || ''}>
                        {current?.source_path || tag.source_path || '未记录'}
                      </td>
                    </tr>
                  )
                })}
                {loading && (
                  <tr><td colSpan={RAW_POINT_COLUMNS.length} className="px-3 py-12 text-center text-gray-400">正在读取原始点位...</td></tr>
                )}
                {!loading && tags.length === 0 && !error && (
                  <tr><td colSpan={RAW_POINT_COLUMNS.length} className="px-3 py-12 text-center text-gray-400">当前节点还没有原始点位</td></tr>
                )}
              </tbody>
            </table>
          </div>

          {totalPages > 1 && (
            <div className="mt-3 flex items-center justify-end gap-2 text-xs text-gray-500">
              <button type="button" disabled={page <= 1} onClick={() => setPage((value) => value - 1)} className="neu-btn px-3 py-1.5 disabled:opacity-40">上一页</button>
              <span>第 {page} / {totalPages} 页</span>
              <button type="button" disabled={page >= totalPages} onClick={() => setPage((value) => value + 1)} className="neu-btn px-3 py-1.5 disabled:opacity-40">下一页</button>
            </div>
          )}
        </>
      )}
    </section>
  )
}
