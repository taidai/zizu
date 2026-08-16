import type { AlarmBlocker, EntityInstance, LegacyAlarmMigrationCandidate } from '../../api/client'
import { formatAlarmConditionValue } from './alarmConfigurationContracts'

interface Props {
  candidates: LegacyAlarmMigrationCandidate[]
  entities: EntityInstance[]
  selections: Record<string, string>
  onSelectionChange: (kind: LegacyAlarmMigrationCandidate['source_kind'], key: string, id: string) => void
  onMigrate: () => void
  migrating: boolean
}

const severity: Record<string, string> = { CRITICAL: '严重', MAJOR: '重要', WARNING: '警告', INFO: '提示' }
const operator: Record<string, string> = { gt: '大于', gte: '大于等于', lt: '小于', lte: '小于等于', eq: '等于', ne: '不等于' }
const blockerText: Record<string, string> = {
  ALARM_MIGRATION_AMBIGUOUS: '需要确认实体实例。',
  ALARM_ENTITY_UNRESOLVED: '未找到可用的已确认实体实例。',
  ALARM_FAULT_MAP_UNRESOLVED: '故障映射无法确认。',
  ALARM_LEGACY_RULE_UNSUPPORTED: '旧规则无法安全转换。',
  ALARM_MIGRATION_SELECTION_INVALID: '所选实体实例不可用于此配置。',
  ALARM_SEVERITY_INVALID: '旧配置的严重度无效。',
  ALARM_THRESHOLD_INVALID: '旧规则阈值无效。',
}

const condition = (value: { operator: string; value: number | string | boolean }) => `${operator[value.operator] || '比较'} ${formatAlarmConditionValue(value.value)}`
const reason = (code: string) => blockerText[code] || '配置存在阻断，请查看诊断信息。'
const ambiguous = (candidate: LegacyAlarmMigrationCandidate) => candidate.blockers.some((blocker) => blocker.code === 'ALARM_MIGRATION_AMBIGUOUS')
const diagnostic = (blockers: AlarmBlocker[]) => blockers.map((blocker) => blocker.message).join('；')

export default function LegacyMigrationPanel({ candidates, entities, selections, onSelectionChange, onMigrate, migrating }: Props) {
  const names = new Map(entities.map((item) => [item.id, `${item.device_display_name} / ${item.display_name}`]))
  const pendingCandidates = candidates.filter((candidate) => candidate.status !== 'migrated')
  const selectedProposal = (candidate: LegacyAlarmMigrationCandidate) => {
    const selected = selections[`${candidate.source_kind}:${candidate.source_key}`] || candidate.entity_instance_id || ''
    return candidate.proposed_rules.find((item) => item.entity_instance_id === selected) || null
  }
  const validChoice = (candidate: LegacyAlarmMigrationCandidate) => {
    const proposal = selectedProposal(candidate)
    return Boolean(proposal && proposal.blockers.length === 0)
  }
  const blocked = pendingCandidates.some((candidate) => candidate.status === 'blocked' && (
    !ambiguous(candidate) || candidate.blockers.length !== 1 || !validChoice(candidate)
  ))

  return (
    <section aria-labelledby="legacy-migration-heading" className="neu-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 id="legacy-migration-heading" className="text-sm font-bold text-gray-800">旧配置迁移</h3>
          <p className="mt-0.5 text-xs text-gray-500">旧配置只读。选择实体后会列出全部将创建的定义；存在候选级阻断的实体不可选择。</p>
        </div>
        <button type="button" onClick={onMigrate} disabled={migrating || !pendingCandidates.length || blocked} className="neu-btn px-3 py-1.5 text-xs font-medium text-[#287c12] disabled:opacity-50">
          {migrating ? '正在生成计划...' : !pendingCandidates.length ? '没有待迁移项' : blocked ? '处理阻断项后可生成计划' : '生成迁移计划'}
        </button>
      </div>
      {!candidates.length ? <p className="mt-4 text-center text-xs text-gray-400">没有待迁移的旧告警配置。</p> : (
        <div className="mt-3 space-y-2">
          {candidates.map((candidate) => {
            const key = `${candidate.source_kind}:${candidate.source_key}`
            const selected = selections[key] || candidate.entity_instance_id || ''
            const needsSelection = ambiguous(candidate)
            const proposal = selectedProposal(candidate)
            return (
              <article key={key} className="rounded border border-white/70 p-3 text-xs">
                <div className="flex flex-wrap justify-between gap-2">
                  <strong className="text-gray-800">{candidate.display_name || '旧告警配置'}</strong>
                  <span className={candidate.status === 'migrated' || candidate.status === 'ready' ? 'text-[#287c12]' : 'text-amber-800'}>
                    {candidate.status === 'migrated' ? '已完成' : candidate.status === 'ready' ? '可迁移' : '需要处理'}
                  </span>
                </div>
                {needsSelection ? (
                  <label className="mt-2 block text-gray-700">确认实体实例
                    <select value={selected} onChange={(event) => onSelectionChange(candidate.source_kind, candidate.source_key, event.target.value)} className="neu-input mt-1 block w-full px-2 py-1">
                      <option value="">请选择实体实例</option>
                      {candidate.proposed_rules.map((item) => (
                        <option key={item.entity_instance_id} value={item.entity_instance_id} disabled={item.blockers.length > 0}>
                          {names.get(item.entity_instance_id) || item.display_name || '已确认实体实例'}{item.blockers.length ? `（不可用：${reason(item.blockers[0].code)}）` : ''}
                        </option>
                      ))}
                    </select>
                  </label>
                ) : selected ? <p className="mt-1 text-gray-600">关联实体：{names.get(selected) || proposal?.display_name || '已确认实体实例'}</p> : null}
                {proposal ? (
                  <div className="mt-2 space-y-2">
                    <p className="font-medium text-gray-700">将创建 {proposal.proposed_definitions.length} 条定义</p>
                    {proposal.proposed_definitions.map((definition, index) => (
                      <div key={`${definition.name}-${index}`} className="rounded border border-gray-100 bg-white/50 p-2" data-diagnostic={diagnostic(definition.blockers)}>
                        <strong className="text-gray-800">{definition.name}</strong>
                        {definition.trigger && definition.recovery && definition.severity ? (
                          <p className="mt-1 text-gray-600">{severity[definition.severity]}，触发{condition(definition.trigger)}，恢复{condition(definition.recovery)}；触发持续 {definition.trigger_duration_seconds} 秒，恢复持续 {definition.recovery_duration_seconds} 秒，通知间隔 {definition.notification_throttle_seconds} 秒</p>
                        ) : <p className="mt-1 text-amber-800">该定义无法安全转换。</p>}
                        {definition.blockers.map((blocker) => <p key={blocker.code} className="mt-1 text-amber-800">{reason(blocker.code)}</p>)}
                      </div>
                    ))}
                    {proposal.blockers.map((blocker) => <p key={blocker.code} data-diagnostic={blocker.message} className="text-amber-800">{reason(blocker.code)}</p>)}
                  </div>
                ) : needsSelection ? <p className="mt-2 text-gray-500">请选择可用实体实例后查看全部拟定定义。</p> : null}
                {candidate.blockers.map((blocker) => <p key={blocker.code} data-diagnostic={blocker.message} className="mt-1 text-amber-800">{reason(blocker.code)}</p>)}
              </article>
            )
          })}
        </div>
      )}
    </section>
  )
}
