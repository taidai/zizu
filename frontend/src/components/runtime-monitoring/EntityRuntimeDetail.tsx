import { useEffect, useMemo, useState } from 'react'
import {
  fetchEntityInstanceHistory,
  fetchEntityInstanceRealtime,
  type EntityHistoryRange,
  type EntityInstance,
  type EntityInstanceObservation,
} from '../../api/client'
import type { L2FrameItem } from '../../api/committedFrameStream'
import { entityHistoryModel, runtimeEntityReading } from './runtimeModel'

const RANGES: Array<[EntityHistoryRange, string]> = [
  ['1h', '1小时'],
  ['6h', '6小时'],
  ['24h', '24小时'],
  ['7d', '7天'],
]

function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未记录'
}

function formatValue(value: unknown): string {
  if (Array.isArray(value)) return value.join('、')
  return value == null ? '—' : String(value)
}

function qualityLabel(quality: number | null | undefined): string {
  if (quality === 192) return '正常'
  if (quality === 64) return '超时'
  if (quality === 1) return '未知'
  if (quality == null) return '无数据'
  return '异常'
}

function HistoryChart({ segments, label }: {
  segments: Array<Array<{ time: number; value: number }>>
  label: string
}) {
  const points = segments.flat()
  if (points.length === 0) return <div className="runtime-empty">该时间范围没有正常数值采样。</div>
  const minTime = Math.min(...points.map((point) => point.time))
  const maxTime = Math.max(...points.map((point) => point.time))
  const minValue = Math.min(...points.map((point) => point.value))
  const maxValue = Math.max(...points.map((point) => point.value))
  const timeSpan = maxTime - minTime || 1
  const valueSpan = maxValue - minValue || 1
  const coordinates = (segment: Array<{ time: number; value: number }>) => segment.map((point) => {
    const x = 4 + ((point.time - minTime) / timeSpan) * 92
    const y = 92 - ((point.value - minValue) / valueSpan) * 84
    return `${x},${y}`
  }).join(' ')
  return (
    <div className="runtime-chart" role="img" aria-label={label}>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none">
        <line x1="4" x2="96" y1="92" y2="92" className="runtime-chart__axis" />
        <line x1="4" x2="96" y1="50" y2="50" className="runtime-chart__grid" />
        {segments.map((segment, index) => (
          segment.length === 1
            ? <circle key={index} cx={coordinates(segment).split(',')[0]} cy={coordinates(segment).split(',')[1]} r="1.4" className="runtime-chart__point" />
            : <polyline key={index} points={coordinates(segment)} className="runtime-chart__line" />
        ))}
      </svg>
      <div className="runtime-chart__legend"><span>{minValue}</span><span>{maxValue}</span></div>
    </div>
  )
}

export default function EntityRuntimeDetail({
  descriptor,
  observation,
  nodeCurrent,
  onClose,
}: {
  descriptor: EntityInstance
  observation: L2FrameItem | null
  nodeCurrent: boolean
  onClose: () => void
}) {
  const [range, setRange] = useState<EntityHistoryRange>('1h')
  const [history, setHistory] = useState<EntityInstanceObservation[]>([])
  const [evidence, setEvidence] = useState<EntityInstanceObservation | null>(null)
  const [historyError, setHistoryError] = useState('')
  const [evidenceError, setEvidenceError] = useState('')
  const [loading, setLoading] = useState(true)
  const [generation, setGeneration] = useState(0)

  useEffect(() => {
    let active = true
    setLoading(true)
    setHistory([])
    setEvidence(null)
    setHistoryError('')
    setEvidenceError('')
    Promise.allSettled([
      fetchEntityInstanceHistory(descriptor.id, range),
      fetchEntityInstanceRealtime(descriptor.id),
    ]).then(([historyResult, evidenceResult]) => {
      if (!active) return
      if (historyResult.status === 'fulfilled') setHistory(historyResult.value)
      else setHistoryError(historyResult.reason instanceof Error ? historyResult.reason.message : '读取实体历史失败。')
      if (evidenceResult.status === 'fulfilled') setEvidence(evidenceResult.value)
      else setEvidenceError(evidenceResult.reason instanceof Error ? evidenceResult.reason.message : '读取实体来源证据失败。')
      setLoading(false)
    })
    return () => { active = false }
  }, [descriptor.id, generation, range])

  const historyModel = useMemo(() => entityHistoryModel(descriptor, history), [descriptor, history])
  const reading = runtimeEntityReading(observation, nodeCurrent)
  const numeric = ['float', 'int'].includes(descriptor.data_type.toLowerCase())

  return (
    <div className="runtime-detail-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.currentTarget === event.target) onClose()
    }}>
      <section role="dialog" aria-modal="true" aria-labelledby="runtime-detail-title" className="runtime-detail neu-card">
        <header className="runtime-detail__header">
          <div>
            <p className="runtime-eyebrow">{descriptor.node_display_name} · {descriptor.definition_id}</p>
            <h3 id="runtime-detail-title">{descriptor.display_name}</h3>
          </div>
          <button type="button" className="neu-btn runtime-touch-button" onClick={onClose}>关闭</button>
        </header>

        <div className="runtime-detail__reading neu-inset">
          <div>
            <span className={`runtime-quality runtime-quality--${reading.quality ?? 'unknown'}`}>{qualityLabel(reading.quality)}</span>
            <p>{reading.kind === 'current' ? '当前值' : reading.kind === 'last' ? '最后值（非当前）' : '当前无值'}</p>
          </div>
          <strong className="font-mono-value">{formatValue(reading.value)}{descriptor.unit ? <small> {descriptor.unit}</small> : null}</strong>
          <dl>
            <div><dt>状态时间</dt><dd>{formatTime(reading.observedAt)}</dd></div>
            <div><dt>最后值时间</dt><dd>{formatTime(reading.valueObservedAt)}</dd></div>
          </dl>
        </div>

        <div className="runtime-detail__tabs" aria-label="历史时间范围">
          {RANGES.map(([value, label]) => (
            <button key={value} type="button" onClick={() => setRange(value)} className={range === value ? 'zizu-tab-active' : 'neu-btn'}>{label}</button>
          ))}
        </div>

        <div className="runtime-detail__body">
          <section aria-label="实体历史">
            <h4>历史</h4>
            {loading ? <div className="runtime-empty">正在读取历史与来源证据…</div> : historyError ? (
              <div className="runtime-error"><span>{historyError}</span><button type="button" onClick={() => setGeneration((value) => value + 1)}>重试历史</button></div>
            ) : numeric ? (
              <HistoryChart segments={historyModel.numericSegments} label={`${descriptor.display_name} ${range}历史趋势`} />
            ) : historyModel.points.length > 0 ? (
              <div className="runtime-history-list">
                {historyModel.points.slice(-20).reverse().map((point, index) => (
                  <div key={`${point.event_id || point.observed_at}-${index}`}>
                    <time>{formatTime(point.observed_at)}</time>
                    <strong>{formatValue(point.value)}</strong>
                    <span>{qualityLabel(point.quality)}</span>
                  </div>
                ))}
              </div>
            ) : <div className="runtime-empty">该时间范围暂无状态历史。</div>}
          </section>

          <section aria-label="实体来源证据">
            <h4>来源与帧证据</h4>
            <dl className="runtime-evidence">
              <div><dt>实体实例</dt><dd>{descriptor.id}</dd></div>
              <div><dt>节点</dt><dd>{descriptor.node_id}</dd></div>
              <div><dt>提交帧</dt><dd>{observation?.frame_sequence ?? '未记录'}</dd></div>
              <div><dt>配置修订</dt><dd>{observation?.configuration_revision ?? '未记录'}</dd></div>
              <div><dt>加工修订</dt><dd>{observation?.processing_revision_id || '未记录'}</dd></div>
              <div><dt>来源摘要</dt><dd>{observation?.source_digest || '未记录'}</dd></div>
            </dl>
            {evidenceError ? (
              <div className="runtime-error"><span>{evidenceError}</span><button type="button" onClick={() => setGeneration((value) => value + 1)}>重试来源</button></div>
            ) : evidence ? (
              <div className="runtime-evidence-note">
                逐实体证据查询：{qualityLabel(evidence.quality)} · {formatTime(evidence.observed_at)}
                <br />来源摘要：{typeof evidence.source_summary === 'string' ? evidence.source_summary : evidence.source_summary?.digest || evidence.source_digest || '未记录'}
                <br />此查询不与节点提交帧拼接为同一帧。
              </div>
            ) : null}
          </section>
        </div>
      </section>
    </div>
  )
}
