import { useMemo } from 'react'
import type { EntityInstance, LegacyAlarmMigrationCandidate } from '../../api/client'

interface Props {
  candidates: LegacyAlarmMigrationCandidate[]
  entities: EntityInstance[]
  selections: Record<string, string>
  onSelectionChange: (sourceKind: LegacyAlarmMigrationCandidate['source_kind'], sourceKey: string, entityInstanceId: string) => void
  onMigrate: () => void
  migrating: boolean
}

const SEVERITY_LABELS: Record<string, string> = { CRITICAL: '严重', MAJOR: '重要', WARNING: '警告', INFO: '提示' }

function statusLabel(candidate: LegacyAlarmMigrationCandidate): string {
  if (candidate.status === 'ready') return '可迁移'
  if (candidate.status === 'ambiguous') return '需要确认实体'
  return '已阻断'
}

export default function LegacyMigrationPanel({ candidates, entities, selections, onSelectionChange, onMigrate, migrating }: Props) {
  const names = useMemo(() => new Map(entities.map((entity) => [entity.id, `${entity.device_display_name} / ${entity.display_name}`])), [entities])
  const hasBlockers = candidates.some((candidate) => candidate.status === 'blocked' || candidate.blockers.length > 0 || (candidate.status === 'ambiguous' && !selections[`${candidate.source_kind}:${candidate.source_key}`]))

  return (
    <section aria-labelledby="legacy-migration-heading" className="neu-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 id="legacy-migration-heading" className="text-sm font-bold text-gray-800">旧配置迁移</h3>
          <p className="mt-0.5 text-xs text-gray-500">旧告警配置只读展示。歧义对象必须明确选择已确认实体实例，不会自动猜测。</p>
        </div>
        <button type="button" onClick={onMigrate} disabled={migrating || candidates.length === 0 || hasBlockers} className="neu-btn px-3 py-1.5 text-xs font-medium text-[#287c12] disabled:cursor-not-allowed disabled:opacity-50">{migrating ? '迁移中...' : hasBlockers ? '处理阻断项后可迁移' : '执行可迁移项'}</button>
      </div>
      {candidates.length === 0 ? <p className="mt-4 rounded-lg border border-dashed border-gray-300 px-3 py-5 text-center text-xs text-gray-400">没有待迁移的旧告警配置。</p> : (
        <div className="mt-3 overflow-x-auto rounded-lg border border-white/70">
          <table className="w-full min-w-[720px] text-left text-xs">
            <thead className="bg-white/20 text-[11px] text-gray-500"><tr className="border-b border-gray-200"><th className="px-3 py-2">来源</th><th className="px-3 py-2">拟定严重度</th><th className="px-3 py-2">关联实体</th><th className="px-3 py-2">迁移状态</th></tr></thead>
            <tbody>
              {candidates.map((candidate) => {
                const selectionKey = `${candidate.source_kind}:${candidate.source_key}`
                const selectedId = selections[selectionKey] || candidate.entity_instance_id || ''
                return <tr key={selectionKey} className="border-b border-white/70 last:border-b-0"><td className="px-3 py-2 font-medium text-gray-700">{candidate.display_name || '旧告警配置'}</td><td className="px-3 py-2 text-gray-600">{candidate.severity ? SEVERITY_LABELS[candidate.severity] : '待确认'}</td><td className="px-3 py-2">{candidate.status === 'ambiguous' ? <select aria-label={`${candidate.display_name} 的实体实例`} value={selectedId} onChange={(event) => onSelectionChange(candidate.source_kind, candidate.source_key, event.target.value)} className="neu-input min-w-56 px-2 py-1 text-xs"><option value="">请选择实体实例</option>{candidate.entity_instance_candidates.map((id) => <option key={id} value={id}>{names.get(id) || '已确认实体实例'}</option>)}</select> : <span className="text-gray-600">{selectedId ? names.get(selectedId) || '已确认实体实例' : '尚未解析'}</span>}</td><td className={candidate.status === 'ready' ? 'px-3 py-2 text-[#287c12]' : 'px-3 py-2 text-amber-800'}>{statusLabel(candidate)}{candidate.blockers.length > 0 && <span className="ml-1 text-gray-500">需检查映射与实体绑定</span>}</td></tr>
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
