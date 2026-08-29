import { useEffect, useMemo, useState } from 'react'
import {
  applyPointProcessingPlan,
  createPointProcessingDraftPlan,
  type PointProcessingPlan,
  type Tag,
} from '../../api/client'
import { buildDataTrunkViewModel } from './dataTrunkViewModel'
import {
  buildNodePointProcessingDraft,
  suggestInlinePointProcessingDefaults,
  type InlinePointProcessingMode,
} from './inlinePointProcessingModel'

export default function InlinePointProcessingPanel({
  nodeId,
  deviceCategory,
  points,
  onPublished,
}: {
  nodeId: string
  deviceCategory: string
  points: Tag[]
  onPublished: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [mode, setMode] = useState<InlinePointProcessingMode>('passthrough')
  const [displayName, setDisplayName] = useState('')
  const [definitionKey, setDefinitionKey] = useState('')
  const [unit, setUnit] = useState('')
  const [dataType, setDataType] = useState('FLOAT')
  const [freshnessSeconds, setFreshnessSeconds] = useState('10')
  const [scale, setScale] = useState('1')
  const [offset, setOffset] = useState('0')
  const [entries, setEntries] = useState('0=STOPPED\n1=RUNNING')
  const [expression, setExpression] = useState('')
  const [plan, setPlan] = useState<PointProcessingPlan | null>(null)
  const [idempotencyKey, setIdempotencyKey] = useState('')
  const [busy, setBusy] = useState<'plan' | 'apply' | null>(null)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const pointIdentity = points.map((point) => point.id).join(',')

  useEffect(() => {
    setPlan(null)
    setIdempotencyKey('')
    setError('')
    setSuccess('')
  }, [nodeId, pointIdentity])

  const openEditor = () => {
    if (points.length === 0) return
    const defaults = suggestInlinePointProcessingDefaults(points, deviceCategory)
    setDisplayName(defaults.displayName)
    setDefinitionKey(defaults.definitionKey)
    setUnit(defaults.unit)
    setDataType(defaults.dataType)
    setExpression(defaults.expression)
    setMode(defaults.mode)
    setExpanded(true)
  }

  const draft = useMemo(() => {
    try {
      return buildNodePointProcessingDraft(points, {
        mode,
        definitionKey,
        displayName,
        deviceCategory,
        dataType,
        unit: unit || null,
        freshnessSeconds: Number(freshnessSeconds),
        scale,
        offset,
        entries,
        expression,
      })
    } catch {
      return null
    }
  }, [dataType, definitionKey, deviceCategory, displayName, entries, expression, freshnessSeconds, mode, offset, points, scale, unit])

  const handlePlan = async () => {
    setBusy('plan')
    setError('')
    setSuccess('')
    try {
      const nextDraft = buildNodePointProcessingDraft(points, {
        mode,
        definitionKey,
        displayName,
        deviceCategory,
        dataType,
        unit: unit || null,
        freshnessSeconds: Number(freshnessSeconds),
        scale,
        offset,
        entries,
        expression,
      })
      const nextPlan = await createPointProcessingDraftPlan(nodeId, {
        content: nextDraft.content,
        input_selections: nextDraft.inputSelections,
      })
      setPlan(nextPlan)
      setIdempotencyKey(crypto.randomUUID())
    } catch (reason) {
      setPlan(null)
      setError(reason instanceof Error ? reason.message : '检查点位加工失败')
    } finally {
      setBusy(null)
    }
  }

  const handleApply = async () => {
    if (!plan || !idempotencyKey) return
    setBusy('apply')
    setError('')
    try {
      await applyPointProcessingPlan(plan.id, plan.digest, idempotencyKey)
      setSuccess('实体已发布，可到“实体数据”查看实时值、历史和来源。')
      setPlan(null)
      onPublished()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '发布实体失败')
    } finally {
      setBusy(null)
    }
  }

  const planView = plan ? buildDataTrunkViewModel({ plan }) : null

  return (
    <div className="mb-3 rounded-lg border border-blue-100 bg-blue-50/60 p-3" aria-label="加工为实体">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <span className="text-xs font-semibold text-gray-800">已选择 {points.length} 个原始点位</span>
          <span className="ml-2 text-[11px] text-gray-500">L0 保持不变，发布后生成稳定实体。</span>
        </div>
        <button
          type="button"
          disabled={points.length === 0}
          onClick={expanded ? () => setExpanded(false) : openEditor}
          className="rounded bg-blue-700 px-4 py-2 text-xs font-semibold text-white disabled:bg-gray-300"
        >
          {expanded ? '收起' : '加工为实体'}
        </button>
      </div>

      {expanded && (
        <div className="mt-3 border-t border-blue-100 pt-3">
          <div className="grid gap-3 lg:grid-cols-3">
            <label className="text-[11px] font-medium text-gray-700">
              实体名称
              <input value={displayName} onChange={(event) => { setDisplayName(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs" />
            </label>
            <label className="text-[11px] font-medium text-gray-700">
              加工方法
              <select value={mode} onChange={(event) => { setMode(event.target.value as InlinePointProcessingMode); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs">
                <option value="passthrough">直接使用</option>
                <option value="numeric" disabled={points.length !== 1}>倍率与偏移</option>
                <option value="state" disabled={points.length !== 1}>状态映射</option>
                <option value="formula">公式计算</option>
              </select>
            </label>
            <label className="text-[11px] font-medium text-gray-700">
              单位
              <input value={unit} onChange={(event) => { setUnit(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs" placeholder="无单位可留空" />
            </label>
          </div>

          {mode === 'numeric' && (
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <label className="text-[11px] font-medium text-gray-700">倍率<input value={scale} onChange={(event) => { setScale(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs" /></label>
              <label className="text-[11px] font-medium text-gray-700">偏移<input value={offset} onChange={(event) => { setOffset(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs" /></label>
            </div>
          )}
          {mode === 'state' && (
            <label className="mt-3 block text-[11px] font-medium text-gray-700">
              状态映射（每行“原值=标准状态”）
              <textarea value={entries} onChange={(event) => { setEntries(event.target.value); setPlan(null) }} rows={3} className="neu-input mt-1 w-full px-3 py-2 font-mono text-xs" />
            </label>
          )}
          {mode === 'formula' && (
            <div className="mt-3">
              <label className="text-[11px] font-medium text-gray-700">
                公式
                <input value={expression} onChange={(event) => { setExpression(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 font-mono text-xs" />
              </label>
            </div>
          )}

          <details className="mt-3 rounded border border-blue-100 bg-white/60 px-3 py-2">
            <summary className="cursor-pointer text-[11px] font-semibold text-gray-600">高级设置</summary>
            <div className="mt-3 grid gap-3 lg:grid-cols-3">
              <label className="text-[11px] font-medium text-gray-700">
                业务标识
                <input value={definitionKey} onChange={(event) => { setDefinitionKey(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 font-mono text-xs" />
              </label>
              <label className="text-[11px] font-medium text-gray-700">
                结果类型
                <select value={dataType} onChange={(event) => { setDataType(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs">
                  {['FLOAT', 'INT', 'BOOL', 'STRING'].map((value) => <option key={value}>{value}</option>)}
                </select>
              </label>
              <label className="text-[11px] font-medium text-gray-700">
                超时秒数
                <input value={freshnessSeconds} onChange={(event) => { setFreshnessSeconds(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs" />
              </label>
            </div>
          </details>

          {error && <div role="alert" className="mt-3 rounded bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>}
          {success && <div className="mt-3 rounded bg-green-50 px-3 py-2 text-xs text-green-700">{success}</div>}
          {planView && (
            <div className={`mt-3 rounded px-3 py-2 text-xs ${planView.canApply ? 'bg-green-50 text-green-800' : 'bg-amber-50 text-amber-800'}`}>
              {planView.canApply ? '检查通过，可以发布。' : planView.nextAction}
            </div>
          )}
          <div className="mt-3 flex justify-end gap-2">
            <button type="button" disabled={busy !== null || !draft} onClick={() => void handlePlan()} className="neu-btn px-4 py-2 text-xs font-semibold text-blue-700 disabled:opacity-50">{busy === 'plan' ? '检查中…' : '检查结果'}</button>
            <button type="button" disabled={busy !== null || !planView?.canApply} onClick={() => void handleApply()} className="rounded bg-[#52c41a] px-4 py-2 text-xs font-semibold text-white disabled:bg-gray-300">{busy === 'apply' ? '发布中…' : '发布实体'}</button>
          </div>
        </div>
      )}
    </div>
  )
}
