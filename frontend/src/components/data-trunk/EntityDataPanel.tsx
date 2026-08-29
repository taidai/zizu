import { useState } from 'react'
import type {
  EntityHistoryRange,
  EntityInstance,
  EntityInstanceObservation,
  NodeDataTrunk,
} from '../../api/client'
import { promotePointProcessingTemplate } from '../../api/client'
import EntityObservationCard from './EntityObservationCard'
import type { CommittedFrameProjection } from './committedFrameProjection'

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
  const entityRows = trunk.l2
    .map((item) => ({ ...item, descriptor: descriptors.get(item.entity_instance_id) }))
    .filter((item): item is typeof item & { descriptor: EntityInstance } => Boolean(item.descriptor))

  return (
    <section className="rounded-xl border border-gray-200 bg-white/55 p-4" aria-label="标准实体列表">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">实体实时数据</h3>
          <p className="mt-1 text-xs text-gray-500">点击一个实体查看它的历史、来源和技术证据。</p>
        </div>
        <div className="flex items-start gap-3">
          {canManageTemplates && trunk.l1_summary.can_promote && (
            <button type="button" onClick={() => setShowPromotion((value) => !value)} className="neu-btn px-3 py-2 text-[11px] font-medium text-blue-700">
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
        <form
          className="mt-4 rounded-lg border border-blue-100 bg-blue-50/60 p-3"
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
            }).then(() => {
              setPromotionMessage('已保存为共享模板；当前节点运行配置没有改变。')
            }).catch((reason: unknown) => {
              setPromotionError(reason instanceof Error ? reason.message : '保存共享模板失败')
            }).finally(() => setPromotionBusy(false))
          }}
        >
          <div className="text-xs font-semibold text-gray-800">管理员复用</div>
          <p className="mt-1 text-[11px] text-gray-500">仅复制当前加工方法到模板库，不切换节点、不改变实体。</p>
          <div className="mt-3 grid gap-2 md:grid-cols-4">
            <input required value={templateName} onChange={(event) => setTemplateName(event.target.value)} placeholder="模板名称" className="neu-input px-3 py-2 text-xs" />
            <input required value={assetId} onChange={(event) => setAssetId(event.target.value)} placeholder="模板标识，如 pcs.site" className="neu-input px-3 py-2 font-mono text-xs" />
            <input required value={brand} onChange={(event) => setBrand(event.target.value)} placeholder="品牌" className="neu-input px-3 py-2 text-xs" />
            <input required value={model} onChange={(event) => setModel(event.target.value)} placeholder="型号" className="neu-input px-3 py-2 text-xs" />
          </div>
          {promotionError && <div role="alert" className="mt-2 text-xs text-red-700">{promotionError}</div>}
          {promotionMessage && <div className="mt-2 text-xs text-green-700">{promotionMessage}</div>}
          <div className="mt-3 flex justify-end">
            <button type="submit" disabled={promotionBusy} className="rounded bg-blue-700 px-4 py-2 text-xs font-semibold text-white disabled:bg-gray-300">{promotionBusy ? '保存中…' : '确认保存'}</button>
          </div>
        </form>
      )}

      <div className="mt-4 space-y-2">
        {entityRows.map((item) => {
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
        {entityRows.length === 0 && (
          <div className="rounded border border-dashed border-gray-300 px-4 py-10 text-center text-xs text-gray-500">
            当前节点还没有标准实体。请到“原始数据”勾选点位并定义数据来源与计算。
          </div>
        )}
      </div>
    </section>
  )
}
