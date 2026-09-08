import { useEffect, useRef, useState } from 'react'
import type {
  EntityHistoryRange,
  EntityInstance,
  EntityInstanceObservation,
  NodeDataTrunk,
} from '../../api/client'
import { promotePointProcessingTemplate } from '../../api/client'
import EntityObservationCard from './EntityObservationCard'
import type { CommittedFrameProjection } from './committedFrameProjection'
import { paginateEntityRows } from './dataTrunkViewModel'

interface EntityDataPanelProps {
  nodeId: string
  canManageTemplates: boolean
  trunk: NodeDataTrunk
  descriptors: Map<string, EntityInstance>
  projection: CommittedFrameProjection | null
  selectedEntityId: string | null
  selectedRange: EntityHistoryRange
  history: EntityInstanceObservation[]
  historyLoading: boolean
  onTemplatePromoted?: (revisionId: string) => void
  onSelectEntity: (entityId: string) => void
  onRangeChange: (range: EntityHistoryRange) => void
}

function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString('zh-CN') : '等待数据'
}

export default function EntityDataPanel({
  nodeId,
  canManageTemplates,
  trunk,
  descriptors,
  projection,
  selectedEntityId,
  selectedRange,
  history,
  historyLoading,
  onTemplatePromoted,
  onSelectEntity,
  onRangeChange,
}: EntityDataPanelProps) {
  const [showPromotion, setShowPromotion] = useState(false)
  const [assetId, setAssetId] = useState('')
  const [templateName, setTemplateName] = useState('')
  const [brand, setBrand] = useState('')
  const [model, setModel] = useState('')
  const [promotionBusy, setPromotionBusy] = useState(false)
  const [promotionMessage, setPromotionMessage] = useState('')
  const [promotionError, setPromotionError] = useState('')
  const [promotionDirty, setPromotionDirty] = useState(false)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<10 | 20>(10)
  const promotionTriggerRef = useRef<HTMLButtonElement | null>(null)
  const promotionNameRef = useRef<HTMLInputElement | null>(null)
  const entityRows = trunk.l2
    .map((item) => ({ ...item, descriptor: descriptors.get(item.entity_instance_id) }))
    .filter((item): item is typeof item & { descriptor: EntityInstance } => Boolean(item.descriptor))
  const pagedRows = paginateEntityRows(entityRows, page, pageSize)

  const closePromotion = () => {
    if (promotionBusy) return
    if (promotionDirty && !window.confirm('放弃尚未保存的共享模板信息？')) return
    setShowPromotion(false)
    setPromotionDirty(false)
    requestAnimationFrame(() => promotionTriggerRef.current?.focus())
  }
  const closePromotionRef = useRef(closePromotion)
  closePromotionRef.current = closePromotion

  useEffect(() => {
    setPage(1)
    setShowPromotion(false)
  }, [nodeId])

  useEffect(() => {
    if (!showPromotion) return
    promotionNameRef.current?.focus()
  }, [showPromotion])

  useEffect(() => {
    if (!showPromotion) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closePromotionRef.current()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [showPromotion])

  const clearEntitySelection = () => {
    if (selectedEntityId) onSelectEntity(selectedEntityId)
  }

  return (
    <section className="engineering-panel rounded-xl p-4" aria-label="标准实体列表">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">实体实时数据</h3>
          <p className="mt-1 text-xs text-gray-500">点击一个实体查看它的历史、来源和技术证据。</p>
        </div>
        <div className="flex items-start gap-3">
          {canManageTemplates && trunk.l1_summary.can_promote && (
            <button type="button" onClick={(event) => {
              promotionTriggerRef.current = event.currentTarget
              setPromotionMessage('')
              setPromotionError('')
              setPromotionDirty(false)
              setShowPromotion(true)
            }} className="neu-btn engineering-touch px-3 text-[11px] font-medium text-[#7d1b23]">
              保存为共享模板
            </button>
          )}
          <div className="text-right text-[10px] text-gray-500">
            <div>{entityRows.length} 个实体</div>
            <div className="mt-1">数据时间 {formatTime(projection?.frameTime)}</div>
          </div>
        </div>
      </div>

      {showPromotion && (
        <div className="engineering-modal-backdrop" role="presentation">
        <form
          className="neu-card engineering-modal w-[620px] max-w-[94vw] p-5"
          role="dialog"
          aria-modal="true"
          aria-labelledby="promotion-title"
          onSubmit={(event) => {
            event.preventDefault()
            setPromotionBusy(true)
            setPromotionError('')
            setPromotionMessage('')
            void promotePointProcessingTemplate(nodeId, {
              asset_id: assetId.trim(),
              display_name: templateName.trim(),
              brand: brand.trim(),
              model: model.trim(),
            }).then((result) => {
              setPromotionMessage('已保存为共享模板；当前节点运行配置没有改变。')
              setPromotionDirty(false)
              onTemplatePromoted?.(result.revision_id)
            }).catch((reason: unknown) => {
              setPromotionError(reason instanceof Error ? reason.message : '保存共享模板失败')
            }).finally(() => setPromotionBusy(false))
          }}
        >
          <div id="promotion-title" className="text-sm font-semibold text-gray-800">保存为共享模板</div>
          <p className="mt-1 text-[11px] text-gray-500">仅复制当前加工方法到模板库，不切换节点、不改变实体。</p>
          <div className="mt-3 grid gap-2 md:grid-cols-4">
            <input ref={promotionNameRef} required value={templateName} onChange={(event) => { setTemplateName(event.target.value); setPromotionDirty(true) }} placeholder="模板名称" className="neu-input px-3 py-2 text-xs" />
            <input required value={assetId} onChange={(event) => { setAssetId(event.target.value); setPromotionDirty(true) }} placeholder="模板标识，如 pcs.site" className="neu-input px-3 py-2 font-mono text-xs" />
            <input required value={brand} onChange={(event) => { setBrand(event.target.value); setPromotionDirty(true) }} placeholder="品牌" className="neu-input px-3 py-2 text-xs" />
            <input required value={model} onChange={(event) => { setModel(event.target.value); setPromotionDirty(true) }} placeholder="型号" className="neu-input px-3 py-2 text-xs" />
          </div>
          {promotionError && <div role="alert" className="mt-2 text-xs text-red-700">{promotionError}</div>}
          {promotionMessage && <div className="mt-2 text-xs text-green-700">{promotionMessage}</div>}
          <div className="mt-3 flex justify-end gap-2">
            <button type="button" disabled={promotionBusy} onClick={closePromotion} className="neu-btn engineering-touch px-4 text-xs disabled:opacity-40">取消</button>
            <button type="submit" disabled={promotionBusy} className="neu-btn zizu-primary engineering-touch px-4 text-xs font-semibold disabled:bg-gray-300">{promotionBusy ? '保存中…' : '确认保存'}</button>
          </div>
        </form>
        </div>
      )}

      <div className="mt-4">
        <div
          className="hidden items-center gap-2 border-b border-gray-200 px-3 pb-2 text-[10px] font-semibold text-gray-500 md:grid md:grid-cols-[minmax(10rem,2fr)_minmax(5rem,1fr)_4rem_5rem_minmax(8rem,1.2fr)_minmax(9rem,1.3fr)]"
        >
          <span>实体名称</span>
          <span>当前值</span>
          <span>单位</span>
          <span>质量</span>
          <span>数据时间</span>
          <span>来源 / 加工</span>
        </div>
        {entityRows.length > 0 ? (
          <div role="list" aria-label="标准实体实时数据" className="mt-2 space-y-2">
            {pagedRows.items.map((item) => {
              const expanded = selectedEntityId === item.entity_instance_id
              return (
                <EntityObservationCard
                  key={item.entity_instance_id}
                  descriptor={item.descriptor}
                  observation={projection?.l2.get(item.entity_instance_id) || null}
                  processingKind={item.processing_kind}
                  sourceSummary={item.source_summary}
                  projectionFrameSequence={projection?.frameSequence ?? null}
                  expanded={expanded}
                  selectedRange={selectedRange}
                  history={expanded ? history : []}
                  historyLoading={expanded && historyLoading}
                  onToggle={() => onSelectEntity(item.entity_instance_id)}
                  onRangeChange={onRangeChange}
                />
              )
            })}
          </div>
        ) : (
          <div className="mt-2 rounded border border-dashed border-gray-300 px-4 py-10 text-center text-xs text-gray-500">
            当前节点还没有标准实体。请到“原始数据”勾选点位并定义数据来源与计算。
          </div>
        )}
      </div>
      {entityRows.length > 0 && (
        <div className="mt-4 flex items-center justify-end gap-2 text-xs text-gray-500">
          <label className="flex items-center gap-2">每页
            <select value={pageSize} onChange={(event) => { clearEntitySelection(); setPageSize(Number(event.target.value) as 10 | 20); setPage(1) }} className="neu-input engineering-touch px-2 text-xs">
              <option value={10}>10 条</option>
              <option value={20}>20 条</option>
            </select>
          </label>
          <button type="button" disabled={pagedRows.page <= 1} onClick={() => { clearEntitySelection(); setPage(pagedRows.page - 1) }} className="neu-btn engineering-touch px-3 disabled:opacity-40">上一页</button>
          <span>第 {pagedRows.page} / {pagedRows.totalPages} 页</span>
          <button type="button" disabled={pagedRows.page >= pagedRows.totalPages} onClick={() => { clearEntitySelection(); setPage(pagedRows.page + 1) }} className="neu-btn engineering-touch px-3 disabled:opacity-40">下一页</button>
        </div>
      )}
    </section>
  )
}
