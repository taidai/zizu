import type {
  EntityHistoryRange,
  EntityInstance,
  EntityInstanceObservation,
  NodeDataTrunk,
} from '../../api/client'
import EntityObservationCard from './EntityObservationCard'
import type { CommittedFrameProjection } from './committedFrameProjection'

interface EntityDataPanelProps {
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
  const entityRows = trunk.l2
    .map((item) => ({ ...item, descriptor: descriptors.get(item.entity_instance_id) }))
    .filter((item): item is typeof item & { descriptor: EntityInstance } => Boolean(item.descriptor))

  return (
    <section className="rounded-xl border border-gray-200 bg-white/55 p-4" aria-label="实体数据列表">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">实体实时数据</h3>
          <p className="mt-1 text-xs text-gray-500">点击一个实体查看它的历史、来源和技术证据。</p>
        </div>
        <div className="text-right text-[10px] text-gray-500">
          <div>{entityRows.length} 个实体</div>
          <div className="mt-1">数据时间 {formatTime(projection?.frameTime)}</div>
        </div>
      </div>

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
              frameSequence={projection?.frameSequence ?? null}
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
            当前节点还没有实体。请先到“点位加工”检查并发布。
          </div>
        )}
      </div>
    </section>
  )
}
