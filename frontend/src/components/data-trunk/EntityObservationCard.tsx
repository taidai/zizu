import ReactECharts from 'echarts-for-react'
import type {
  EntityHistoryRange,
  EntityInstance,
  EntityInstanceObservation,
  NodeDataTrunk,
} from '../../api/client'
import type { L2FrameItem } from '../../api/committedFrameStream'
import {
  entityReasonLabel,
  ENTITY_HISTORY_RANGES,
  processingKindLabel,
  qualityLabel,
} from './dataTrunkViewModel'

const QUALITY_STYLE: Record<number, string> = {
  192: 'border-green-200 bg-green-50 text-green-700',
  64: 'border-amber-200 bg-amber-50 text-amber-700',
  1: 'border-gray-300 bg-gray-100 text-gray-600',
  0: 'border-red-200 bg-red-50 text-red-700',
}

function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString('zh-CN') : '未收到'
}

function valueLabel(value: L2FrameItem['value']): string {
  if (value === null) return '-'
  if (Array.isArray(value)) return value.join('、')
  return String(value)
}

export default function EntityObservationCard({
  descriptor,
  observation,
  processingKind,
  sourceSummary,
  expanded,
  selectedRange,
  history,
  historyLoading,
  onToggle,
  onRangeChange,
}: {
  descriptor: EntityInstance
  observation: L2FrameItem | null
  processingKind: string | null
  sourceSummary: NodeDataTrunk['l1_summary']['source_summary']
  expanded: boolean
  selectedRange: EntityHistoryRange
  history: EntityInstanceObservation[]
  historyLoading: boolean
  onToggle: () => void
  onRangeChange: (range: EntityHistoryRange) => void
}) {
  const quality = observation?.quality ?? 1
  const ageMs = observation?.observed_at
    ? Math.max(0, Date.now() - new Date(observation.observed_at).getTime())
    : 0
  const reason = entityReasonLabel(observation?.reason ?? (observation ? null : 'STALE'), ageMs)
  const numericHistory = history.some((item) => typeof item.value === 'number')
  const chartData = history.map((item) => [
    item.observed_at,
    item.quality === 192 && typeof item.value === 'number' ? item.value : null,
  ])

  return (
    <article className="rounded-lg border border-gray-200 bg-white">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="grid w-full items-center gap-2 px-3 py-3 text-left text-xs hover:bg-gray-50 md:grid-cols-[minmax(10rem,2fr)_minmax(5rem,1fr)_5rem_6rem_minmax(9rem,1.2fr)_5rem]"
      >
        <span className="min-w-0 font-semibold text-gray-900">
          <span className="block truncate">{descriptor.display_name}</span>
        </span>
        <span className={`font-mono-value text-base font-semibold ${quality === 192 ? 'text-gray-900' : 'text-gray-500'}`}>
          {valueLabel(observation?.value ?? null)}
        </span>
        <span className="text-gray-500">{observation?.unit || descriptor.unit || '-'}</span>
        <span className={`w-fit rounded border px-2 py-0.5 text-[10px] font-semibold ${QUALITY_STYLE[quality] || QUALITY_STYLE[0]}`}>
          {qualityLabel(quality)}
        </span>
        <span className="text-gray-500">{formatTime(observation?.observed_at)}</span>
        <span className="text-gray-600">{processingKindLabel(processingKind)}</span>
      </button>

      {expanded && (
        <div className="border-t border-gray-200 px-3 py-4">
          {reason && (
            <div className="mb-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              {reason}
            </div>
          )}

          <div className="grid gap-4 xl:grid-cols-[minmax(0,1.5fr)_minmax(14rem,0.8fr)]">
            <section aria-label="实体历史">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h5 className="text-xs font-semibold text-gray-800">历史</h5>
                <div className="flex flex-wrap gap-1">
                  {ENTITY_HISTORY_RANGES.map(([range, label]) => (
                    <button
                      key={range}
                      type="button"
                      onClick={() => onRangeChange(range)}
                      className={`rounded px-2 py-1 text-[10px] ${
                        selectedRange === range
                          ? 'bg-[#52c41a] text-white'
                          : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>

              {historyLoading ? (
                <div className="mt-3 flex h-28 items-center justify-center text-xs text-gray-400">正在读取历史...</div>
              ) : numericHistory && history.length > 1 ? (
                <div className="mt-3 h-36" aria-label={`${descriptor.display_name}历史趋势`}>
                  <ReactECharts
                    style={{ height: 144 }}
                    option={{
                      animation: false,
                      grid: { left: 44, right: 10, top: 10, bottom: 24 },
                      xAxis: { type: 'time', axisLabel: { fontSize: 9 } },
                      yAxis: { type: 'value', scale: true, axisLabel: { fontSize: 9 } },
                      tooltip: { trigger: 'axis', valueFormatter: (value: unknown) => String(value ?? '无') },
                      series: [{
                        type: 'line',
                        data: chartData,
                        connectNulls: false,
                        showSymbol: false,
                        lineStyle: { width: 1.5, color: '#2563eb' },
                      }],
                    }}
                    notMerge
                    lazyUpdate
                  />
                </div>
              ) : history.length > 0 ? (
                <div className="mt-3 max-h-32 overflow-y-auto rounded bg-gray-50 p-2 text-[10px] text-gray-600">
                  {history.slice(-20).reverse().map((item, index) => (
                    <div key={`${item.event_id || item.observed_at}-${index}`} className="flex justify-between gap-3 py-1">
                      <span>{formatTime(item.observed_at)}</span>
                      <span className="font-medium">{Array.isArray(item.value) ? item.value.join('、') : String(item.value ?? '无')}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="mt-3 flex h-28 items-center justify-center rounded bg-gray-50 text-xs text-gray-400">该时间范围暂无历史数据</div>
              )}
            </section>

            <section className="text-xs" aria-label="实体来源">
              <h5 className="font-semibold text-gray-800">来源</h5>
              <p className="mt-2 leading-5 text-gray-600">
                来源：{sourceSummary.map((item) => item.source_key).join('、') || '等待来源'}
              </p>
              <p className="leading-5 text-gray-600">加工：{processingKindLabel(processingKind)}</p>

              <details className="mt-3 rounded border border-gray-200 bg-gray-50 p-3 text-[10px] text-gray-600">
                <summary className="cursor-pointer font-semibold text-gray-800">技术详情</summary>
                <dl className="mt-2 space-y-1 break-all font-mono">
                  <div><dt className="inline text-gray-400">definition_id: </dt><dd className="inline">{descriptor.definition_id}</dd></div>
                  <div><dt className="inline text-gray-400">processing_revision_id: </dt><dd className="inline">{observation?.processing_revision_id || '未记录'}</dd></div>
                  <div><dt className="inline text-gray-400">configuration_revision: </dt><dd className="inline">{observation?.configuration_revision ?? '未记录'}</dd></div>
                  <div><dt className="inline text-gray-400">source_digest: </dt><dd className="inline">{observation?.source_digest || '未记录'}</dd></div>
                  <div><dt className="inline text-gray-400">frame_sequence: </dt><dd className="inline">{observation?.frame_sequence ?? '未记录'}</dd></div>
                  <div><dt className="inline text-gray-400">received_at: </dt><dd className="inline">{formatTime(observation?.received_at)}</dd></div>
                  <div><dt className="inline text-gray-400">calculated_at: </dt><dd className="inline">{formatTime(observation?.calculated_at)}</dd></div>
                  <div><dt className="inline text-gray-400">reason: </dt><dd className="inline">{observation?.reason || '无'}</dd></div>
                </dl>
              </details>
            </section>
          </div>
        </div>
      )}
    </article>
  )
}
