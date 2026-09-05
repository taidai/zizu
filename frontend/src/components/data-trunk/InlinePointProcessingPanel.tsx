import { useEffect, useState } from 'react'
import {
  applyPointProcessingPlan,
  createPointProcessingDraftPlan,
  type PointProcessingPlan,
  type Tag,
} from '../../api/client'
import { buildDataTrunkViewModel } from './dataTrunkViewModel'
import {
  buildNodePointProcessingDraft,
  canDeclareInlinePassthroughUnit,
  projectInlinePointProcessingTrial,
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
  const [trueWhen, setTrueWhen] = useState<'0' | '1'>('1')
  const [controlEnabled, setControlEnabled] = useState(false)
  const [controlMinimum, setControlMinimum] = useState('')
  const [controlMaximum, setControlMaximum] = useState('')
  const [controlTolerance, setControlTolerance] = useState('0.1')
  const [controlCooldownSeconds, setControlCooldownSeconds] = useState('5')
  const [controlTimeoutSeconds, setControlTimeoutSeconds] = useState('15')
  const [plan, setPlan] = useState<PointProcessingPlan | null>(null)
  const [idempotencyKey, setIdempotencyKey] = useState('')
  const [busy, setBusy] = useState<'plan' | 'apply' | null>(null)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const pointIdentity = points.map((point) => point.id).join(',')
  const booleanMapEligible = points.length === 1
    && points[0].wire_data_type?.toUpperCase() === 'BIT'
    && points[0].data_type.toUpperCase() === 'INT'
  const controlEligible = points.length === 1
    && points[0].read_write.toUpperCase() === 'RW'
    && ['FLOAT', 'INT'].includes(points[0].data_type.toUpperCase())
  const canDeclareUnit = canDeclareInlinePassthroughUnit(points, mode)

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
    setTrueWhen('1')
    setControlEnabled(false)
    setControlMinimum('')
    setControlMaximum('')
    setExpanded(true)
  }

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
        trueWhen: Number(trueWhen),
        controlEnabled,
        controlMinimum,
        controlMaximum,
        controlTolerance,
        controlCooldownSeconds,
        controlTimeoutSeconds,
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
      setSuccess('标准实体已发布，可到“标准实体”查看实时值、历史和来源。')
      setPlan(null)
      onPublished()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '发布实体失败')
    } finally {
      setBusy(null)
    }
  }

  const planView = plan ? buildDataTrunkViewModel({ plan }) : null
  const trialView = plan?.trial
    ? projectInlinePointProcessingTrial(plan.trial, definitionKey)
    : null

  return (
    <div className="mb-3 rounded-lg border border-blue-100 bg-blue-50/60 p-3" aria-label="加工为实体">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <span className="text-xs font-semibold text-gray-800">已选择 {points.length} 个原始点位</span>
          <span className="ml-2 text-[11px] text-gray-500">定义标准实体的数据来源与计算，原始数据保持不变。</span>
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
              <select value={mode} onChange={(event) => {
                const nextMode = event.target.value as InlinePointProcessingMode
                setMode(nextMode)
                if (nextMode !== 'passthrough') setControlEnabled(false)
                if (nextMode === 'boolean_map') {
                  setDataType('BOOL')
                  setUnit('')
                } else if (nextMode === 'passthrough' && points[0]) {
                  setDataType(points[0].data_type.toUpperCase())
                  setUnit(points[0].unit || '')
                }
                setPlan(null)
              }} className="neu-input mt-1 w-full px-3 py-2 text-xs">
                <option value="passthrough">直接使用</option>
                <option value="boolean_map" disabled={!booleanMapEligible}>0/1 转布尔</option>
                <option value="numeric" disabled={points.length !== 1}>倍率与偏移</option>
                <option value="state" disabled={points.length !== 1}>状态映射</option>
                <option value="formula">公式计算</option>
              </select>
            </label>
            <label className="text-[11px] font-medium text-gray-700">
              单位
              <input disabled={mode === 'boolean_map' || (mode === 'passthrough' && !canDeclareUnit)} value={unit} onChange={(event) => { setUnit(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs disabled:bg-gray-100" placeholder="无单位可留空" />
              {canDeclareUnit && <span className="mt-1 block font-normal text-amber-700">原始数值未声明单位；仅填写真实工程单位，直接使用不会缩放或猜测数值。</span>}
            </label>
          </div>

          {mode === 'boolean_map' && (
            <div className="mt-3 rounded border border-blue-100 bg-white px-3 py-3">
              <label className="text-[11px] font-medium text-gray-700">
                哪个原值表示 true
                <select value={trueWhen} onChange={(event) => { setTrueWhen(event.target.value as '0' | '1'); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs sm:w-56">
                  <option value="1">1 表示 true（推荐）</option>
                  <option value="0">0 表示 true</option>
                </select>
              </label>
              <p className="mt-2 text-[11px] text-gray-600">
                设备原值 {points[0]?.raw_value ?? '—'} → 原值等于 {trueWhen} → 实体值 {typeof points[0]?.raw_value === 'number' ? String(points[0].raw_value === Number(trueWhen)) : '等待试算'}
              </p>
            </div>
          )}

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

          <div className="mt-3 rounded border border-blue-100 bg-white px-3 py-3">
            <label className="flex items-center gap-2 text-xs font-semibold text-gray-800">
              <input
                type="checkbox"
                checked={controlEnabled}
                disabled={!controlEligible || mode !== 'passthrough'}
                onChange={(event) => { setControlEnabled(event.target.checked); setPlan(null) }}
              />
              允许调度控制
            </label>
            <p className="mt-1 text-[11px] text-gray-500">
              仅单个 RW 原始点位“直接使用”时可开启；调度策略只写这个已确认点位。
            </p>
            {controlEnabled && (
              <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
                <label className="text-[11px] font-medium text-gray-700">安全最小值<input value={controlMinimum} onChange={(event) => { setControlMinimum(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs" /></label>
                <label className="text-[11px] font-medium text-gray-700">安全最大值<input value={controlMaximum} onChange={(event) => { setControlMaximum(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs" /></label>
                <label className="text-[11px] font-medium text-gray-700">回读容差<input value={controlTolerance} onChange={(event) => { setControlTolerance(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs" /></label>
                <label className="text-[11px] font-medium text-gray-700">冷却秒数<input value={controlCooldownSeconds} onChange={(event) => { setControlCooldownSeconds(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs" /></label>
                <label className="text-[11px] font-medium text-gray-700">回读期限（秒）<input value={controlTimeoutSeconds} onChange={(event) => { setControlTimeoutSeconds(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs" /></label>
              </div>
            )}
          </div>

          <details className="mt-3 rounded border border-blue-100 bg-white/60 px-3 py-2">
            <summary className="cursor-pointer text-[11px] font-semibold text-gray-600">高级设置</summary>
            <div className="mt-3 grid gap-3 lg:grid-cols-3">
              <label className="text-[11px] font-medium text-gray-700">
                业务标识
                <input value={definitionKey} onChange={(event) => { setDefinitionKey(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 font-mono text-xs" />
              </label>
              <label className="text-[11px] font-medium text-gray-700">
                结果类型
                <select disabled={mode === 'passthrough' || mode === 'boolean_map'} value={dataType} onChange={(event) => { setDataType(event.target.value); setPlan(null) }} className="neu-input mt-1 w-full px-3 py-2 text-xs disabled:bg-gray-100">
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
          {trialView && (
            <div className={`mt-3 rounded border px-3 py-3 text-xs ${trialView.status === 'available' ? 'border-green-200 bg-green-50 text-green-900' : 'border-amber-200 bg-amber-50 text-amber-900'}`}>
              <div className="font-semibold">当前试算结果</div>
              {trialView.status === 'available' ? (
                <div className="mt-2 grid gap-2 sm:grid-cols-3">
                  <div><span className="text-gray-500">当前值</span><div className="mt-1 font-mono-value text-base font-semibold">{trialView.valueText}</div></div>
                  <div><span className="text-gray-500">质量</span><div className="mt-1 font-semibold">{trialView.qualityText}</div></div>
                  <div><span className="text-gray-500">来源证据</span><div className="mt-1">{trialView.evidenceText}</div></div>
                  <div className="sm:col-span-3 text-[11px] text-gray-500">数据时间：{trialView.observedAt ? new Date(trialView.observedAt).toLocaleString() : '—'}{trialView.message ? ` · ${trialView.message}` : ''}</div>
                </div>
              ) : (
                <p className="mt-2">{trialView.message}</p>
              )}
            </div>
          )}
          <div className="mt-3 flex justify-end gap-2">
            <button type="button" disabled={busy !== null} onClick={() => void handlePlan()} className="neu-btn px-4 py-2 text-xs font-semibold text-blue-700 disabled:opacity-50">{busy === 'plan' ? '检查中…' : '检查结果'}</button>
            <button type="button" disabled={busy !== null || !planView?.canApply} onClick={() => void handleApply()} className="rounded bg-[#52c41a] px-4 py-2 text-xs font-semibold text-white disabled:bg-gray-300">{busy === 'apply' ? '发布中…' : '发布实体'}</button>
          </div>
        </div>
      )}
    </div>
  )
}
