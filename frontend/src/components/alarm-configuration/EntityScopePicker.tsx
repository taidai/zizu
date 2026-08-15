import { useMemo, useState } from 'react'
import type { EntityInstance } from '../../api/client'

export interface AlarmEntityScope {
  entity_instance_ids: string[]
  device_instance_ids: string[]
  entity_definition_ids: string[]
}

interface Props {
  entities: EntityInstance[]
  value: AlarmEntityScope
  onChange: (value: AlarmEntityScope) => void
  disabled?: boolean
}

function toggle(values: string[], value: string): string[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value]
}

export default function EntityScopePicker({ entities, value, onChange, disabled }: Props) {
  const [query, setQuery] = useState('')
  const visibleEntities = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase('zh-CN')
    if (!normalized) return entities
    return entities.filter((item) => [item.display_name, item.device_display_name, item.definition_id, item.instance_key]
      .filter(Boolean).some((text) => text.toLocaleLowerCase('zh-CN').includes(normalized)))
  }, [entities, query])
  const devices = useMemo(() => Array.from(new Map(entities.map((item) => [item.device_instance_id, item.device_display_name])).entries()), [entities])
  const definitions = useMemo(() => Array.from(new Map(entities.map((item) => [item.definition_id, item.display_name])).entries()).sort((left, right) => left[1].localeCompare(right[1], 'zh-CN')), [entities])

  const update = (patch: Partial<AlarmEntityScope>) => onChange({ ...value, ...patch })
  const selectedCount = new Set(value.entity_instance_ids).size + new Set(value.device_instance_ids).size + new Set(value.entity_definition_ids).size

  return (
    <section aria-labelledby="alarm-scope-heading" className="neu-card p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h3 id="alarm-scope-heading" className="text-sm font-bold text-gray-800">配置范围</h3>
          <p className="mt-0.5 text-xs text-gray-500">可按实体实例、设备组或实体定义叠加选择，系统将在计划中展开实际作用范围。</p>
        </div>
        <span className="rounded-md bg-[#52c41a]/10 px-2 py-1 text-xs font-medium text-[#287c12]">已选择 {selectedCount} 类范围</span>
      </div>

      <div className="mt-3 grid gap-3 lg:grid-cols-[1fr_1fr_2fr]">
        <div className="neu-inset p-3">
          <div className="mb-2 text-xs font-semibold text-gray-700">设备组</div>
          <div className="max-h-44 space-y-1 overflow-y-auto pr-1">
            {devices.map(([id, name]) => (
              <label key={id} className="flex cursor-pointer items-center gap-2 rounded px-1 py-1 text-xs text-gray-700 hover:bg-white/50">
                <input type="checkbox" disabled={disabled} checked={value.device_instance_ids.includes(id)} onChange={() => update({ device_instance_ids: toggle(value.device_instance_ids, id) })} className="h-4 w-4 accent-[#52c41a]" />
                <span className="truncate">{name || '未命名设备'}</span>
              </label>
            ))}
            {devices.length === 0 && <p className="py-3 text-center text-xs text-gray-400">暂无已确认设备实例</p>}
          </div>
        </div>

        <div className="neu-inset p-3">
          <div className="mb-2 text-xs font-semibold text-gray-700">实体定义</div>
          <div className="max-h-44 space-y-1 overflow-y-auto pr-1">
            {definitions.map(([definitionId, displayName]) => (
              <label key={definitionId} className="flex cursor-pointer items-center gap-2 rounded px-1 py-1 text-xs text-gray-700 hover:bg-white/50">
                <input type="checkbox" disabled={disabled} checked={value.entity_definition_ids.includes(definitionId)} onChange={() => update({ entity_definition_ids: toggle(value.entity_definition_ids, definitionId) })} className="h-4 w-4 accent-[#52c41a]" />
                <span className="truncate">{displayName || '未命名实体定义'}</span>
              </label>
            ))}
            {definitions.length === 0 && <p className="py-3 text-center text-xs text-gray-400">暂无可用实体定义</p>}
          </div>
        </div>

        <div className="neu-inset p-3">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
            <label className="text-xs font-semibold text-gray-700" htmlFor="alarm-entity-search">实体实例</label>
            <input id="alarm-entity-search" value={query} onChange={(event) => setQuery(event.target.value)} className="neu-input w-full max-w-[220px] px-2 py-1 text-xs" placeholder="筛选显示名称或设备" disabled={disabled} />
          </div>
          <div className="max-h-44 overflow-y-auto rounded border border-white/60 bg-white/20">
            {visibleEntities.map((entity) => (
              <label key={entity.id} className="flex cursor-pointer items-center gap-2 border-b border-white/60 px-2 py-1.5 text-xs last:border-b-0 hover:bg-white/50">
                <input type="checkbox" disabled={disabled} checked={value.entity_instance_ids.includes(entity.id)} onChange={() => update({ entity_instance_ids: toggle(value.entity_instance_ids, entity.id) })} className="h-4 w-4 accent-[#52c41a]" />
                <span className="min-w-0 flex-1 truncate font-medium text-gray-700">{entity.display_name}</span>
                <span className="max-w-[36%] truncate text-[11px] text-gray-500">{entity.device_display_name}</span>
                {entity.unit && <span className="text-[11px] text-gray-400">{entity.unit}</span>}
              </label>
            ))}
            {visibleEntities.length === 0 && <p className="px-3 py-5 text-center text-xs text-gray-400">没有匹配的已确认实体实例</p>}
          </div>
        </div>
      </div>
    </section>
  )
}
