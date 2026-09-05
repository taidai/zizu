const ERROR_MESSAGES = {
  OUTPUT_LIMIT_VIOLATION: '功率目标超出该实体允许的控制范围。',
  OUTPUT_NOT_CONTROLLABLE: '请选择允许控制的全局实体。',
  OUTPUT_WRITE_POINT_UNCONFIRMED: '该实体没有唯一、已确认的设备写点。',
  OUTPUT_ALREADY_OWNED: '该实体已被另一条启用策略占用。',
  STRATEGY_INPUT_REQUIRED: '请绑定 SOC 输入实体。',
  STRATEGY_OUTPUT_REQUIRED: '请绑定功率控制实体。',
  L2_BINDING_TYPE_MISMATCH: '实体的数据类型已经变化，请重新选择。',
  L2_BINDING_UNIT_MISMATCH: '实体的单位已经变化，请重新选择。',
  L2_QUALITY_NOT_GOOD: '当前实体数据质量不可用于试算。',
  L2_INPUT_STALE: '当前实体数据已超时，请先检查数据链路。',
  COMMITTED_L2_SNAPSHOT_UNAVAILABLE: '当前还没有完整、已提交的实体快照。',
  STRATEGY_DRAFT_CONFLICT: '策略已被其他人修改，请刷新后重试。',
  CONFIGURATION_REVISION_CHANGED: '实体配置已经变化，请重新保存后发布。',
  SOC_BINDING_DEFINITION_INVALID: 'SOC 输入必须绑定 bms.soc 或 storage.soc 标准实体。',
  SOC_BINDING_TYPE_INVALID: 'SOC 输入必须是 INT 或 FLOAT 数值实体。',
  SOC_BINDING_UNIT_INVALID: 'SOC 输入必须使用百分比（%）单位。',
  SOC_VALUE_INVALID: 'SOC 当前值必须是 0 到 100 之间的有效数字。',
  POWER_TARGET_BINDING_TYPE_INVALID: '功率控制目标必须是 INT 或 FLOAT 数值实体。',
  POWER_TARGET_BINDING_UNIT_INVALID: '功率控制目标必须使用 kW 单位。',
  POWER_TARGET_VALUE_INVALID: '功率目标必须是有效的有限数字。',
}

const FIELD_LABELS = {
  name: '名称',
  site_timezone: '场站时区',
  jdm_content: '决策表',
  bindings: '实体绑定',
  expected_digest: '策略版本',
  configuration_revision: '配置版本',
}

function minute(value, allow24 = false) {
  const match = /^(\d{2}):(\d{2})$/.exec(String(value))
  if (!match) return null
  const hour = Number(match[1])
  const part = Number(match[2])
  if (allow24 && hour === 24 && part === 0) return 1440
  if (hour > 23 || part > 59) return null
  return hour * 60 + part
}

function finite(value) {
  if (value === '' || value === null || value === undefined) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function decimal(value) {
  const number = Number(value)
  return Object.is(number, -0) || number === 0 ? '0' : String(number)
}

export function splitCrossMidnight(window) {
  const start = minute(window.start)
  const end = minute(window.end, true)
  if (start === null || end === null || start <= end) return [{ ...window }]
  return [
    { ...window, key: `${window.key}:late`, end: '24:00' },
    { ...window, key: `${window.key}:early`, start: '00:00' },
  ]
}

export function normalizeDispatchWindows(windows) {
  return windows
    .flatMap(splitCrossMidnight)
    .map((window) => ({ ...window }))
    .sort((left, right) => {
      const byStart = (minute(left.start) ?? 9999) - (minute(right.start) ?? 9999)
      if (byStart) return byStart
      const byEnd = (minute(left.end, true) ?? 9999) - (minute(right.end, true) ?? 9999)
      return byEnd || String(left.key).localeCompare(String(right.key))
    })
}

export function validateDispatchWindows(windows, safeTarget) {
  const safe = finite(safeTarget)
  if (safe === null) {
    return { valid: false, overlapKeys: [], message: '请填写其他时段安全目标。', rows: [] }
  }
  const rows = normalizeDispatchWindows(windows)
  if (rows.length === 0) {
    return { valid: false, overlapKeys: [], message: '请至少配置一个充电或放电时段。', rows }
  }
  const keys = new Set()
  for (const row of rows) {
    const start = minute(row.start)
    const end = minute(row.end, true)
    if (!row.key || keys.has(row.key)) {
      return { valid: false, overlapKeys: [], message: '每个时段需要不同的标识。', rows }
    }
    keys.add(row.key)
    if (start === null || end === null || start >= end) {
      return { valid: false, overlapKeys: [], message: '请检查时段的开始和结束时间。', rows }
    }
    if (!['CHARGE', 'DISCHARGE', 'HOLD'].includes(row.action)) {
      return { valid: false, overlapKeys: [], message: '请选择充电、放电或保持。', rows }
    }
    if ([row.target, row.socMin, row.socMax].some((value) => finite(value) === null)) {
      return { valid: false, overlapKeys: [], message: '功率和 SOC 必须填写数字。', rows }
    }
    if (Number(row.socMin) < 0 || Number(row.socMax) > 100 || Number(row.socMin) > Number(row.socMax)) {
      return { valid: false, overlapKeys: [], message: 'SOC 范围必须在 0 到 100 之间。', rows }
    }
  }
  for (let index = 1; index < rows.length; index += 1) {
    const previous = rows[index - 1]
    const current = rows[index]
    if (minute(current.start) < minute(previous.end, true)) {
      return {
        valid: false,
        overlapKeys: [previous.key, current.key],
        message: '两个调度时间重叠，请调整后再保存。',
        rows,
      }
    }
  }
  return { valid: true, overlapKeys: [], message: '', rows }
}

export function makeStrategyBinding(entity, direction, bindingKey, ordinal) {
  return {
    direction,
    binding_key: bindingKey,
    ordinal,
    entity_instance_id: entity.id,
    expected_data_type: String(entity.data_type).toUpperCase(),
    unit: entity.unit ?? null,
    freshness_seconds: Number(entity.freshness_seconds),
  }
}

export function isDispatchSocEntity(entity) {
  return !!entity?.confirmed
    && ['R', 'RW'].includes(entity.direction)
    && ['bms.soc', 'storage.soc'].includes(entity.definition_id)
    && ['INT', 'FLOAT'].includes(String(entity.data_type).toUpperCase())
    && entity.unit === '%'
}

export function isDispatchPowerTargetEntity(entity) {
  return !!entity?.confirmed
    && ['W', 'RW'].includes(entity.direction)
    && ['INT', 'FLOAT'].includes(String(entity.data_type).toUpperCase())
    && entity.unit === 'kW'
}

export function buildTwoChargeTwoDischargeJdm(windows, safeTarget) {
  const validation = validateDispatchWindows(windows, safeTarget)
  if (!validation.valid) throw new Error(validation.message)
  const rules = validation.rows.map((row) => ({
    _id: row.key,
    site_local_minute: `site_local_minute >= ${minute(row.start)} && site_local_minute < ${minute(row.end, true)}`,
    soc: `soc >= ${decimal(row.socMin)} && soc <= ${decimal(row.socMax)}`,
    action_id: JSON.stringify('power-target'),
    target: decimal(row.target),
    matched_rule: JSON.stringify(row.key),
    _description: row.action,
  }))
  rules.push({
    _id: 'other-time',
    site_local_minute: '1 == 1',
    soc: '1 == 1',
    action_id: JSON.stringify('power-target'),
    target: decimal(safeTarget),
    matched_rule: JSON.stringify('other-time'),
    _description: 'HOLD',
  })
  return {
    nodes: [
      { id: 'input', type: 'inputNode', name: 'Input' },
      {
        id: 'schedule',
        type: 'decisionTableNode',
        name: '2充2放',
        content: {
          hitPolicy: 'first',
          inputs: [
            { id: 'site_local_minute', name: '场站本地分钟', type: 'expression', field: 'site_local_minute' },
            { id: 'soc', name: 'SOC', type: 'expression', field: 'soc' },
          ],
          outputs: [
            { id: 'action_id', name: '动作标识', type: 'expression', field: 'action_id' },
            { id: 'target', name: '功率目标', type: 'expression', field: 'target' },
            { id: 'matched_rule', name: '命中行', type: 'expression', field: 'matched_rule' },
          ],
          rules,
        },
      },
      { id: 'output', type: 'outputNode', name: 'Output' },
    ],
    edges: [
      { id: 'input-schedule', sourceId: 'input', targetId: 'schedule', type: 'edge' },
      { id: 'schedule-output', sourceId: 'schedule', targetId: 'output', type: 'edge' },
    ],
  }
}

export function isJdmGraphUnchanged(left, right) {
  const json = (value) => JSON.stringify(value, (_key, item) => item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b)))
    : item)
  return json(left) === json(right)
}

export function readTwoChargeTwoDischargeJdm(graph) {
  const rules = graph?.nodes?.find((node) => node.id === 'schedule')?.content?.rules
  if (!Array.isArray(rules) || rules.length < 2) return null
  const toTime = (minutes) => `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`
  const rows = []
  for (const rule of rules.slice(0, -1)) {
    const time = /^site_local_minute >= (\d+) && site_local_minute < (\d+)$/.exec(rule?.site_local_minute)
    const soc = /^soc >= (\S+) && soc <= (\S+)$/.exec(rule?.soc)
    if (!time || !soc) return null
    rows.push({
      key: rule._id,
      start: toTime(Number(time[1])),
      end: toTime(Number(time[2])),
      action: rule._description,
      target: Number(rule.target),
      socMin: Number(soc[1]),
      socMax: Number(soc[2]),
    })
  }
  const safeTarget = Number(rules.at(-1)?.target)
  // Only our exact table is editable here. Unknown nodes, expressions and fields
  // stay in the original graph; this is not a general JDM reverse compiler.
  try {
    if (!isJdmGraphUnchanged(graph, buildTwoChargeTwoDischargeJdm(rows, safeTarget))) return null
  } catch {
    return null
  }
  return { rows, safeTarget }
}

export function describeDispatchStrategyError(reason) {
  const payload = reason?.payload || reason
  const detail = payload?.detail
  if (Array.isArray(detail)) {
    const location = detail[0]?.loc
    const field = Array.isArray(location) ? location.at(-1) : null
    return `请检查“${FIELD_LABELS[field] || field || '策略配置'}”后重试。`
  }
  const code = reason?.code || detail?.code
  if (code && ERROR_MESSAGES[code]) return ERROR_MESSAGES[code]
  const message = detail?.message || (typeof detail === 'string' ? detail : null)
  return message || reason?.message || '调度策略操作未完成，请检查配置后重试。'
}

export function projectStrategyStatus(strategy) {
  const draftRevision = strategy.draft?.revision ?? null
  const publishedRevision = strategy.published_revision?.revision ?? null
  const hasUnpublished = draftRevision !== null
    && (
      publishedRevision === null
      || draftRevision > publishedRevision
      || (strategy.draft?.id && strategy.draft.id !== strategy.published_revision?.id)
    )
  const health = {
    READY: '就绪',
    BLOCKED: '阻断',
    FAILED: '故障锁定',
    IDLE: '待运行',
  }[strategy.runtime_health] || strategy.runtime_health || '未知'
  return {
    draftRevision,
    publishedRevision,
    lifecycleLabel: hasUnpublished ? '有未发布修改' : publishedRevision === null ? '未发布' : '已发布',
    enableLabel: strategy.enabled ? '已启用' : '已停用',
    healthLabel: health,
    healthDetail: strategy.failure_code || '',
  }
}
