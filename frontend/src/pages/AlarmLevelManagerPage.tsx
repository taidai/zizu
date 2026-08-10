import { useEffect, useMemo, useState } from 'react'
import {
  fetchAlarmLevels,
  createAlarmLevel,
  updateAlarmLevel,
  deleteAlarmLevel,
  fetchEntities,
  fetchAlarmLevelEntities,
  batchBindEntitiesToAlarmLevel,
  unbindEntityFromAlarmLevel,
  type AlarmLevel,
  type Entity,
  type EntityAlarmBinding,
  type TriggerRule,
} from '../api/client'

const SEVERITY_STYLES: Record<string, string> = {
  CRITICAL: 'bg-red-100 text-red-700 border-red-200',
  MAJOR: 'bg-orange-100 text-orange-700 border-orange-200',
  WARNING: 'bg-amber-100 text-amber-700 border-amber-200',
  INFO: 'bg-blue-100 text-blue-700 border-blue-200',
}

const DEFAULT_COLOR = '#ef4444'

function ruleLabel(rule: TriggerRule): string {
  const map: Record<string, string> = {
    active: '激活即告警',
    eq: `等于 ${rule.value}`,
    ne: `不等于 ${rule.value}`,
    gte: `≥ ${rule.threshold}`,
    gt: `> ${rule.threshold}`,
    lte: `≤ ${rule.threshold}`,
    lt: `< ${rule.threshold}`,
    fault: '故障码匹配',
  }
  return map[rule.op] || rule.op
}

export default function AlarmLevelManagerPage() {
  const [levels, setLevels] = useState<AlarmLevel[]>([])
  const [entities, setEntities] = useState<Entity[]>([])
  const [bindingsByLevel, setBindingsByLevel] = useState<Record<string, EntityAlarmBinding[]>>({})
  const [loading, setLoading] = useState(false)
  const [selectedLevelId, setSelectedLevelId] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [categoryFilter, setCategoryFilter] = useState('')

  // Create/edit modal
  const [editingLevel, setEditingLevel] = useState<AlarmLevel | null>(null)
  const [showModal, setShowModal] = useState(false)
  const [formCode, setFormCode] = useState('')
  const [formName, setFormName] = useState('')
  const [formSeverity, setFormSeverity] = useState<'CRITICAL' | 'MAJOR' | 'WARNING' | 'INFO'>('WARNING')
  const [formColor, setFormColor] = useState(DEFAULT_COLOR)
  const [formRules, setFormRules] = useState<TriggerRule[]>([{ op: 'active' }])

  // Batch bind modal
  const [showBindModal, setShowBindModal] = useState(false)
  const [selectedEntityIds, setSelectedEntityIds] = useState<Set<string>>(new Set())
  const [bindRules, setBindRules] = useState<TriggerRule[]>([])

  const load = async () => {
    setLoading(true)
    try {
      const [lv, en] = await Promise.all([
        fetchAlarmLevels(),
        fetchEntities({ page: 1, page_size: 10000, search: search || undefined, category: categoryFilter || undefined }),
      ])
      setLevels(lv.items)
      setEntities(en.items)
      const bindings: Record<string, EntityAlarmBinding[]> = {}
      await Promise.all(
        lv.items.map(async (l) => {
          const b = await fetchAlarmLevelEntities(l.id)
          bindings[l.id] = b.items
        }),
      )
      setBindingsByLevel(bindings)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [categoryFilter])

  const selectedLevel = useMemo(
    () => levels.find((l) => l.id === selectedLevelId),
    [levels, selectedLevelId],
  )

  const filteredEntities = useMemo(() => {
    const s = search.trim().toLowerCase()
    return entities.filter((e) => {
      const matchSearch = !s || e.name.toLowerCase().includes(s) || (e.display_name && e.display_name.toLowerCase().includes(s))
      const matchCategory = !categoryFilter || e.category === categoryFilter
      return matchSearch && matchCategory
    })
  }, [entities, search, categoryFilter])

  const openCreate = () => {
    setEditingLevel(null)
    setFormCode('')
    setFormName('')
    setFormSeverity('WARNING')
    setFormColor(DEFAULT_COLOR)
    setFormRules([{ op: 'active' }])
    setShowModal(true)
  }

  const openEdit = (level: AlarmLevel) => {
    setEditingLevel(level)
    setFormCode(level.code)
    setFormName(level.name)
    setFormSeverity(level.severity)
    setFormColor(level.color || DEFAULT_COLOR)
    setFormRules(level.trigger_rules.length ? level.trigger_rules : [{ op: 'active' }])
    setShowModal(true)
  }

  const handleSaveLevel = async () => {
    const payload = {
      code: formCode,
      name: formName,
      severity: formSeverity,
      color: formColor,
      trigger_rules: formRules,
      enabled: true,
      sort_order: editingLevel ? editingLevel.sort_order : levels.length,
    }
    try {
      if (editingLevel) {
        await updateAlarmLevel(editingLevel.id, payload)
      } else {
        await createAlarmLevel(payload)
      }
      setShowModal(false)
      load()
    } catch (e: any) {
      alert(e.message)
    }
  }

  const handleDeleteLevel = async (level: AlarmLevel) => {
    if (!confirm(`确定删除告警等级「${level.name}」？绑定关系将一并删除。`)) return
    try {
      await deleteAlarmLevel(level.id)
      load()
    } catch (e: any) {
      alert(e.message)
    }
  }

  const openBind = (levelId: string) => {
    setSelectedLevelId(levelId)
    setSelectedEntityIds(new Set())
    setBindRules([])
    setShowBindModal(true)
  }

  const toggleEntity = (id: string) => {
    setSelectedEntityIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handleBatchBind = async () => {
    if (!selectedLevelId || selectedEntityIds.size === 0) return
    try {
      await batchBindEntitiesToAlarmLevel(
        selectedLevelId,
        Array.from(selectedEntityIds),
        bindRules.length ? bindRules : undefined,
      )
      setShowBindModal(false)
      load()
    } catch (e: any) {
      alert(e.message)
    }
  }

  const handleUnbind = async (levelId: string, bindingId: string) => {
    try {
      await unbindEntityFromAlarmLevel(levelId, bindingId)
      load()
    } catch (e: any) {
      alert(e.message)
    }
  }

  const categories = useMemo(() => {
    const set = new Set<string>()
    entities.forEach((e) => e.category && set.add(e.category))
    return Array.from(set).sort()
  }, [entities])

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-bold text-gray-800">告警等级管理</h2>
          <p className="text-xs text-gray-500">自定义告警等级、触发规则，并批量绑定全局实体。</p>
        </div>
        <button onClick={openCreate} className="neu-btn px-3 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d]">
          + 新建等级
        </button>
      </div>

      {loading && <div className="text-xs text-gray-400">加载中...</div>}

      <div className="space-y-3">
        {levels.map((level) => {
          const bound = bindingsByLevel[level.id] || []
          return (
            <div key={level.id} className="neu-card p-4">
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <span
                    className="w-4 h-4 rounded-full border border-gray-200"
                    style={{ backgroundColor: level.color || '#ccc' }}
                  />
                  <div>
                    <div className="flex items-center gap-2">
                      <h3 className="text-sm font-bold text-gray-800">{level.name}</h3>
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold border ${SEVERITY_STYLES[level.severity]}`}>
                        {level.severity}
                      </span>
                      {level.is_system && (
                        <span className="px-1.5 py-0.5 rounded text-[10px] bg-gray-100 text-gray-500 border border-gray-200">
                          系统
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-gray-500 mt-0.5">
                      code: <span className="font-mono">{level.code}</span>
                      <span className="mx-2">·</span>
                      规则: {level.trigger_rules.map(ruleLabel).join(' / ') || '激活即告警'}
                      <span className="mx-2">·</span>
                      已绑定 {bound.length} 个实体
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => openBind(level.id)}
                    className="neu-btn px-3 py-1 text-xs text-gray-700"
                  >
                    绑定实体
                  </button>
                  {!level.is_system && (
                    <>
                      <button onClick={() => openEdit(level)} className="neu-btn px-3 py-1 text-xs text-gray-600">
                        编辑
                      </button>
                      <button
                        onClick={() => handleDeleteLevel(level)}
                        className="neu-btn px-3 py-1 text-xs text-red-600 hover:bg-red-50"
                      >
                        删除
                      </button>
                    </>
                  )}
                </div>
              </div>

              {bound.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {bound.map((b) => (
                    <span
                      key={b.id}
                      className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] bg-gray-100 text-gray-700 border border-gray-200"
                    >
                      {b.entity_display_name || b.entity_name}
                      <button
                        onClick={() => handleUnbind(level.id, b.id)}
                        className="text-gray-400 hover:text-red-500"
                        title="解绑"
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {showModal && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
          <div className="neu-card bg-[#e8e8e8] w-full max-w-lg max-h-[90vh] overflow-y-auto p-5">
            <h3 className="text-sm font-bold text-gray-800 mb-4">
              {editingLevel ? '编辑告警等级' : '新建告警等级'}
            </h3>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-gray-600 mb-1">编码 *</label>
                  <input
                    value={formCode}
                    onChange={(e) => setFormCode(e.target.value)}
                    className="neu-input w-full px-3 py-1.5 text-xs"
                    placeholder="error1 / level_pcs_fault"
                    disabled={!!editingLevel?.is_system}
                  />
                </div>
                <div>
                  <label className="block text-xs text-gray-600 mb-1">名称 *</label>
                  <input
                    value={formName}
                    onChange={(e) => setFormName(e.target.value)}
                    className="neu-input w-full px-3 py-1.5 text-xs"
                    placeholder="严重告警"
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-gray-600 mb-1">严重度 *</label>
                  <select
                    value={formSeverity}
                    onChange={(e) => setFormSeverity(e.target.value as any)}
                    className="neu-input w-full px-3 py-1.5 text-xs"
                    disabled={editingLevel?.is_system}
                  >
                    <option value="CRITICAL">CRITICAL</option>
                    <option value="MAJOR">MAJOR</option>
                    <option value="WARNING">WARNING</option>
                    <option value="INFO">INFO</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs text-gray-600 mb-1">颜色</label>
                  <div className="flex items-center gap-2">
                    <input
                      type="color"
                      value={formColor}
                      onChange={(e) => setFormColor(e.target.value)}
                      className="w-8 h-8 rounded cursor-pointer border-0 bg-transparent"
                    />
                    <span className="text-xs text-gray-500">{formColor}</span>
                  </div>
                </div>
              </div>

              <div>
                <label className="block text-xs text-gray-600 mb-1">触发规则</label>
                {formRules.map((rule, idx) => (
                  <div key={idx} className="flex items-center gap-2 mb-2">
                    <select
                      value={rule.op}
                      onChange={(e) => {
                        const op = e.target.value as TriggerRule['op']
                        setFormRules((rs) => rs.map((r, i) => (i === idx ? { op } : r)))
                      }}
                      className="neu-input px-2 py-1 text-xs"
                    >
                      <option value="active">激活即告警</option>
                      <option value="eq">等于</option>
                      <option value="ne">不等于</option>
                      <option value="gte">≥</option>
                      <option value="gt">&gt;</option>
                      <option value="lte">≤</option>
                      <option value="lt">&lt;</option>
                      <option value="fault">故障码匹配</option>
                    </select>
                    {rule.op !== 'active' && rule.op !== 'fault' && (
                      <input
                        type="text"
                        value={rule.threshold ?? rule.value ?? ''}
                        onChange={(e) => {
                          const val = e.target.value
                          setFormRules((rs) =>
                            rs.map((r, i) =>
                              i === idx
                                ? { ...r, [rule.op === 'eq' || rule.op === 'ne' ? 'value' : 'threshold']: val }
                                : r,
                            ),
                          )
                        }}
                        className="neu-input flex-1 px-3 py-1 text-xs"
                        placeholder={rule.op === 'eq' || rule.op === 'ne' ? '目标值' : '阈值'}
                      />
                    )}
                    <button
                      onClick={() => setFormRules((rs) => rs.filter((_, i) => i !== idx))}
                      className="text-red-500 text-xs px-2"
                    >
                      删除
                    </button>
                  </div>
                ))}
                <button
                  onClick={() => setFormRules((rs) => [...rs, { op: 'active' }])}
                  className="neu-btn px-3 py-1 text-xs text-gray-600"
                >
                  + 添加规则
                </button>
              </div>
            </div>
            <div className="flex justify-end gap-2 pt-4">
              <button onClick={() => setShowModal(false)} className="neu-btn px-4 py-1.5 text-xs text-gray-600">
                取消
              </button>
              <button
                onClick={handleSaveLevel}
                className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d]"
              >
                保存
              </button>
            </div>
          </div>
        </div>
      )}

      {showBindModal && selectedLevel && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
          <div className="neu-card bg-[#e8e8e8] w-full max-w-3xl max-h-[90vh] overflow-y-auto p-5">
            <h3 className="text-sm font-bold text-gray-800 mb-2">
              绑定实体到「{selectedLevel.name}」
            </h3>
            <p className="text-xs text-gray-500 mb-3">已选 {selectedEntityIds.size} 个实体</p>

            <div className="flex items-center gap-2 mb-3">
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="neu-input flex-1 px-3 py-1.5 text-xs"
                placeholder="搜索实体名..."
              />
              <select
                value={categoryFilter}
                onChange={(e) => setCategoryFilter(e.target.value)}
                className="neu-input px-3 py-1.5 text-xs"
              >
                <option value="">全部分类</option>
                {categories.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>

            <div className="border border-gray-200 rounded bg-white/50 max-h-[300px] overflow-y-auto mb-4">
              {filteredEntities.map((e) => {
                const bound = (bindingsByLevel[selectedLevel.id] || []).some((b) => b.entity_id === e.id)
                return (
                  <label
                    key={e.id}
                    className="flex items-center gap-2 px-3 py-2 hover:bg-indigo-50 cursor-pointer border-b border-gray-50 last:border-0"
                  >
                    <input
                      type="checkbox"
                      checked={selectedEntityIds.has(e.id)}
                      onChange={() => toggleEntity(e.id)}
                      className="w-4 h-4 accent-indigo-500"
                    />
                    <span className="text-xs text-gray-700">{e.display_name || e.name}</span>
                    <span className="text-[10px] text-gray-400 font-mono">{e.name}</span>
                    <span className="text-[10px] text-gray-400 ml-auto">{e.category || '无分类'}</span>
                    {bound && <span className="text-[10px] text-green-600 ml-2">已绑定</span>}
                  </label>
                )
              })}
            </div>

            <div className="mb-4">
              <label className="block text-xs text-gray-600 mb-1">覆盖触发规则（可选，留空使用等级默认规则）</label>
              {bindRules.map((rule, idx) => (
                <div key={idx} className="flex items-center gap-2 mb-2">
                  <select
                    value={rule.op}
                    onChange={(e) => {
                      const op = e.target.value as TriggerRule['op']
                      setBindRules((rs) => rs.map((r, i) => (i === idx ? { op } : r)))
                    }}
                    className="neu-input px-2 py-1 text-xs"
                  >
                    <option value="active">激活即告警</option>
                    <option value="eq">等于</option>
                    <option value="ne">不等于</option>
                    <option value="gte">≥</option>
                    <option value="gt">&gt;</option>
                    <option value="lte">≤</option>
                    <option value="lt">&lt;</option>
                    <option value="fault">故障码匹配</option>
                  </select>
                  {rule.op !== 'active' && rule.op !== 'fault' && (
                    <input
                      type="text"
                      value={rule.threshold ?? rule.value ?? ''}
                      onChange={(e) => {
                        const val = e.target.value
                        setBindRules((rs) =>
                          rs.map((r, i) =>
                            i === idx
                              ? { ...r, [rule.op === 'eq' || rule.op === 'ne' ? 'value' : 'threshold']: val }
                              : r,
                          ),
                        )
                      }}
                      className="neu-input flex-1 px-3 py-1 text-xs"
                      placeholder={rule.op === 'eq' || rule.op === 'ne' ? '目标值' : '阈值'}
                    />
                  )}
                  <button
                    onClick={() => setBindRules((rs) => rs.filter((_, i) => i !== idx))}
                    className="text-red-500 text-xs px-2"
                  >
                    删除
                  </button>
                </div>
              ))}
              <button
                onClick={() => setBindRules((rs) => [...rs, { op: 'active' }])}
                className="neu-btn px-3 py-1 text-xs text-gray-600"
              >
                + 添加覆盖规则
              </button>
            </div>

            <div className="flex justify-end gap-2">
              <button onClick={() => setShowBindModal(false)} className="neu-btn px-4 py-1.5 text-xs text-gray-600">
                取消
              </button>
              <button
                onClick={handleBatchBind}
                disabled={selectedEntityIds.size === 0}
                className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50"
              >
                批量绑定 {selectedEntityIds.size} 个实体
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
