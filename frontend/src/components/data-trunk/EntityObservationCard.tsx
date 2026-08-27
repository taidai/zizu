import ReactECharts from 'echarts-for-react'
import type { EntityInstance, EntityInstanceObservation } from '../../api/client'
import type { L2FrameItem } from '../../api/committedFrameStream'

const QUALITY_STYLE: Record<number, string> = {
  192: 'border-green-200 bg-green-50 text-green-700',
  64: 'border-amber-200 bg-amber-50 text-amber-700',
  1: 'border-gray-300 bg-gray-100 text-gray-600',
  0: 'border-red-200 bg-red-50 text-red-700',
}

const QUALITY_LABEL: Record<number, string> = {
  192: '正常',
  64: '不确定',
  1: '陈旧',
  0: '无效',
}

function ageLabel(ageMs: number): string {
  if (ageMs < 1000) return '刚刚更新'
  if (ageMs < 60_000) return `${Math.floor(ageMs / 1000)} 秒前`
  if (ageMs < 3_600_000) return `${Math.floor(ageMs / 60_000)} 分钟前`
  return `${Math.floor(ageMs / 3_600_000)} 小时前`
}

function revisionLabel(revisionId: string | null): string {
  if (!revisionId) return '未记录'
  return `转换版本 ${revisionId.slice(0, 8)}`
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN') : '未记录'
}

function valueLabel(value: L2FrameItem['value']): string {
  if (value === null) return '—'
  if (Array.isArray(value)) return value.join('、')
  return String(value)
}

export default function EntityObservationCard({
  descriptor,
  observation,
  history,
  frameTime,
}: {
  descriptor: EntityInstance
  observation: L2FrameItem | null
  history: EntityInstanceObservation[]
  frameTime: string | null
}) {
  const quality = observation?.quality ?? 1
  const currentValue = valueLabel(observation?.value ?? null)
  const currentUsable = quality !== 0 && quality !== 1 && observation?.value !== null
  const ageMs = observation?.observed_at
    ? Math.max(0, Date.now() - new Date(observation.observed_at).getTime())
    : null
  const latestGood = [...history].reverse().find((item) => item.quality === 192)
  const numeric = history.some((item) => typeof item.value === 'number')
  const chartData = history.map((item) => [
    item.observed_at,
    (item.quality === 0 || item.quality === 1 || typeof item.value !== 'number') ? null : item.value,
  ])
  const sourceDigest = observation?.source_digest

  return (
    <article className="rounded-xl border border-gray-200 bg-white/60 p-3 shadow-sm shadow-gray-300/20">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h4 className="truncate text-xs font-semibold text-gray-800" title={descriptor.display_name}>
            {descriptor.display_name}
          </h4>
          <p className="mt-0.5 text-[10px] text-gray-500">{descriptor.definition_id}</p>
        </div>
        <span className={`shrink-0 rounded-md border px-2 py-0.5 text-[10px] font-semibold ${QUALITY_STYLE[observation?.quality ?? 1]}`}>
          {QUALITY_LABEL[quality] || `质量 ${quality}`}
        </span>
      </div>

      <div className="mt-3 flex items-baseline gap-1.5">
        <span className={`font-mono-value text-2xl font-semibold ${quality === 1 ? 'text-gray-400' : 'text-gray-900'}`}>{currentValue}</span>
        {observation?.value !== null && descriptor.unit && <span className="text-xs text-gray-500">{descriptor.unit}</span>}
      </div>
      {!currentUsable && quality !== 1 && latestGood && (
        <p className="mt-1 text-[10px] text-gray-500">
          最近正常值 {String(latestGood.value)}{descriptor.unit ? ` ${descriptor.unit}` : ''}（非当前值）
        </p>
      )}
      {observation?.reason && <p className="mt-1 text-[10px] text-red-600">原因：{observation.reason}</p>}

      {numeric && history.length > 1 ? (
        <div className="mt-2 h-20" aria-label={`${descriptor.display_name} 最近一小时趋势`}>
          <ReactECharts
            style={{ height: 80 }}
            option={{
              animation: false,
              grid: { left: 3, right: 3, top: 8, bottom: 3 },
              xAxis: { type: 'time', show: false },
              yAxis: { type: 'value', show: false, scale: true },
              tooltip: { trigger: 'axis', valueFormatter: (value: unknown) => String(value ?? '无') },
              series: [{
                type: 'line',
                data: chartData,
                connectNulls: false,
                showSymbol: false,
                lineStyle: { width: 1.5, color: '#2563eb' },
                areaStyle: { color: 'rgba(37,99,235,0.08)' },
              }],
            }}
            notMerge
            lazyUpdate
          />
        </div>
      ) : history.length > 1 ? (
        <div className="mt-2 flex max-h-16 flex-wrap gap-1 overflow-hidden">
          {history.slice(-6).map((item, index) => (
            <span key={`${item.event_id || item.observed_at}-${index}`} className="rounded-md bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">
              {Array.isArray(item.value) ? item.value.join('、') : String(item.value ?? '无')}
            </span>
          ))}
        </div>
      ) : null}

      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 border-t border-gray-200 pt-2 text-[10px] text-gray-500">
        <div><dt className="inline">数据年龄：</dt><dd className="inline">{ageMs === null ? '等待数据' : ageLabel(ageMs)}</dd></div>
        <div><dt className="inline">统一配置：</dt><dd className="inline">{observation?.configuration_revision ?? '未记录'}</dd></div>
        <div><dt className="inline">加工来源：</dt><dd className="inline">{revisionLabel(observation?.processing_revision_id ?? null)}</dd></div>
        <div title={sourceDigest || undefined}><dt className="inline">来源证据：</dt><dd className="inline">{sourceDigest ? sourceDigest.slice(0, 10) : '未记录'}</dd></div>
        <div><dt className="inline">数据时间：</dt><dd className="inline">{formatTime(observation?.observed_at ?? null)}</dd></div>
        <div><dt className="inline">接收时间：</dt><dd className="inline">{formatTime(observation?.received_at ?? null)}</dd></div>
        <div><dt className="inline">计算时间：</dt><dd className="inline">{formatTime(observation?.calculated_at ?? null)}</dd></div>
        <div><dt className="inline">成帧时间：</dt><dd className="inline">{formatTime(frameTime)}</dd></div>
        <div><dt className="inline">帧序号：</dt><dd className="inline">{observation?.frame_sequence ?? '—'}</dd></div>
      </dl>
    </article>
  )
}
