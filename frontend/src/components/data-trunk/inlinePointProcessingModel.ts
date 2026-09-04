import type { PointProcessingTrial } from '../../api/client'

export type InlinePointProcessingMode = 'passthrough' | 'boolean_map' | 'numeric' | 'state' | 'formula'

export interface InlineRawPoint {
  id: string
  name: string
  display_name: string | null
  wire_data_type: string | null
  data_type: string
  unit: string | null
  read_write: string
}

export interface InlinePointProcessingForm {
  mode: InlinePointProcessingMode
  definitionKey: string
  displayName: string
  deviceCategory: string
  dataType?: string
  unit?: string | null
  freshnessSeconds: number
  scale?: number | string
  offset?: number | string
  minimum?: number | string
  maximum?: number | string
  entries?: string
  expression?: string
  trueWhen?: number
  controlEnabled?: boolean
  controlMinimum?: number | string
  controlMaximum?: number | string
  controlTolerance?: number | string
  controlCooldownSeconds?: number | string
  controlTimeoutSeconds?: number | string
}

export interface InlinePointProcessingDraft {
  content: Record<string, unknown> & {
    inputs: Array<Record<string, unknown> & { id: string }>
    outputs: Array<Record<string, unknown> & {
      id: string
      entityDefinition: string
      transform: Record<string, unknown>
    }>
  }
  inputSelections: Record<string, string>
}

export interface InlinePointProcessingDefaults {
  mode: InlinePointProcessingMode
  displayName: string
  definitionKey: string
  unit: string
  dataType: string
  expression: string
}

export interface InlinePointProcessingTrialView {
  status: 'available' | 'unavailable'
  valueText: string
  qualityText: string
  evidenceText: string
  observedAt: string | null
  message: string
}

const TRIAL_REASON_MESSAGES: Record<string, string> = {
  POINT_PROCESSING_TRIAL_FRAME_UNAVAILABLE: '当前还没有可用于试算的已提交数据帧。',
  POINT_PROCESSING_TRIAL_UNAVAILABLE: '当前数据无法用于试算，请检查原始数据后重试。',
}

function trialQualityLabel(quality: number): string {
  if (quality === 192) return '正常'
  if (quality === 64) return '存疑'
  if (quality === 1) return '超时'
  if (quality === 0) return '无效'
  return '未知质量'
}

function trialValue(output: { value: unknown; unit: string | null }): string {
  if (!output || output.value === null) return '—'
  const raw = Array.isArray(output.value)
    ? output.value.join('、')
    : typeof output.value === 'boolean'
      ? (output.value ? '是' : '否')
      : String(output.value)
  return output.unit ? `${raw} ${output.unit}` : raw
}

export function projectInlinePointProcessingTrial(
  trial: PointProcessingTrial,
  targetEntityDefinitionId?: string,
): InlinePointProcessingTrialView {
  if (!trial.available) {
    return {
      status: 'unavailable',
      valueText: '—',
      qualityText: '未试算',
      evidenceText: '',
      observedAt: null,
      message: TRIAL_REASON_MESSAGES[trial.reason]
        || '当前无法试算，请检查原始数据后重试。',
    }
  }
  const output = targetEntityDefinitionId
    ? trial.outputs.find((item) => item.entity_definition_id === targetEntityDefinitionId)
    : trial.outputs[0]
  if (!output) {
    return {
      status: 'unavailable',
      valueText: '—',
      qualityText: '未试算',
      evidenceText: '',
      observedAt: null,
      message: '检查已完成，但没有生成标准实体结果。',
    }
  }
  return {
    status: 'available',
    valueText: trialValue(output),
    qualityText: trialQualityLabel(output.quality),
    evidenceText: `${output.source_ids.length} 个来源 · 帧 ${trial.frame_sequence} · 配置 ${trial.configuration_revision}`,
    observedAt: output.observed_at,
    message: output.reason || '',
  }
}

function stableKey(value: string, fallback: string): string {
  const normalized = value
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
  return normalized || fallback
}

const FORMULA_RESERVED_IDENTIFIERS = new Set([
  'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue',
  'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from', 'global',
  'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 'pass',
  'raise', 'return', 'try', 'while', 'with', 'yield',
])

function pointInputIds(points: readonly InlineRawPoint[]): string[] {
  const used = new Set<string>()
  return points.map((point, index) => {
    const normalized = stableKey(point.name, `point_${index + 1}`)
    const base = /^[a-z]/.test(normalized) && !FORMULA_RESERVED_IDENTIFIERS.has(normalized)
      ? normalized
      : `point_${normalized}`
    let inputId = base
    let suffix = 2
    while (used.has(inputId)) inputId = `${base}_${suffix++}`
    used.add(inputId)
    return inputId
  })
}

export function suggestInlinePointProcessingDefaults(
  points: readonly InlineRawPoint[],
  deviceCategory: string,
): InlinePointProcessingDefaults {
  if (points.length === 0) throw new Error('至少选择一个原始点位')
  const first = points[0]
  const category = stableKey(deviceCategory, 'entity')
  const sourceKey = stableKey(first.name, '')
  const identityFallback = `point_${stableKey(first.id, 'value')}`
  const isBit = points.length === 1
    && first.wire_data_type?.toUpperCase() === 'BIT'
    && first.data_type.toUpperCase() === 'INT'

  return {
    mode: points.length > 1 ? 'formula' : isBit ? 'boolean_map' : 'passthrough',
    displayName: first.display_name || first.name,
    definitionKey: `${/^[a-z]/.test(category) ? category : `device_${category}`}.${sourceKey || identityFallback}`,
    unit: isBit ? '' : first.unit || '',
    dataType: isBit ? 'BOOL' : first.data_type.toUpperCase(),
    expression: pointInputIds(points).join(' + '),
  }
}

function finiteNumber(value: unknown, fallback: number, label: string): number {
  const candidate = value === undefined || value === null || value === ''
    ? fallback
    : Number(value)
  if (!Number.isFinite(candidate)) throw new Error(`${label}必须是数字`)
  return candidate
}

function requiredFiniteNumber(value: unknown, label: string): number {
  if (value === undefined || value === null || value === '') {
    throw new Error(`请填写控制${label}`)
  }
  return finiteNumber(value, 0, `控制${label}`)
}

function stateEntries(raw: string | undefined): Record<string, string> {
  const entries: Record<string, string> = {}
  for (const rawLine of String(raw ?? '').split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line) continue
    const separator = line.indexOf('=')
    if (separator <= 0 || separator === line.length - 1) {
      throw new Error(`状态映射格式错误：${line}`)
    }
    entries[line.slice(0, separator).trim()] = line.slice(separator + 1).trim()
  }
  if (Object.keys(entries).length === 0) throw new Error('请填写状态映射')
  return entries
}

export function buildNodePointProcessingDraft(
  points: readonly InlineRawPoint[],
  form: InlinePointProcessingForm,
): InlinePointProcessingDraft {
  if (points.length === 0) throw new Error('至少选择一个原始点位')
  if (form.mode === 'boolean_map' && points.length !== 1) {
    throw new Error('0/1 转布尔一次只能选择一个原始点位')
  }
  if (points.length > 1 && form.mode !== 'formula') {
    throw new Error('多点加工请选择公式')
  }
  const definitionKey = form.definitionKey.trim()
  const displayName = form.displayName.trim()
  if (!definitionKey || !/^[a-z][a-z0-9_.]*$/i.test(definitionKey)) {
    throw new Error('实体标识只能使用字母、数字、点和下划线')
  }
  if (!displayName) throw new Error('请填写实体名称')
  const freshnessSeconds = finiteNumber(form.freshnessSeconds, 30, '新鲜时间')
  if (freshnessSeconds <= 0) throw new Error('新鲜时间必须大于 0')

  const inputIds = pointInputIds(points)
  const keyedPoints = points.map((point, index) => ({
    point: { ...point },
    inputId: inputIds[index],
  }))
  const first = keyedPoints[0]
  const definitionParts = definitionKey.split('.')
  const outputId = stableKey(definitionParts[definitionParts.length - 1] || '', 'entity')
  const outputType = form.mode === 'boolean_map'
    ? 'BOOL'
    : form.mode === 'state'
    ? 'ENUM'
    : form.mode === 'numeric'
      ? 'FLOAT'
    : String(form.dataType || first.point.data_type).toUpperCase()
  const outputUnit = form.mode === 'boolean_map'
    ? null
    : form.unit === undefined ? first.point.unit : form.unit
  if (form.mode === 'passthrough' && (
    outputType !== first.point.data_type.toUpperCase()
    || (outputUnit || null) !== (first.point.unit || null)
  )) {
    throw new Error('直接使用不能改变单位，请选择倍率与偏移')
  }
  let transform: Record<string, unknown>
  if (form.mode === 'boolean_map') {
    const point = first.point
    const trueWhen = form.trueWhen ?? 1
    if (
      point.wire_data_type?.toUpperCase() !== 'BIT'
      || point.data_type.toUpperCase() !== 'INT'
      || point.unit !== null
    ) {
      throw new Error('0/1 转布尔只适用于 BIT 协议的整数点位')
    }
    if (trueWhen !== 0 && trueWhen !== 1) {
      throw new Error('请选择 0 或 1 表示 true')
    }
    transform = {
      kind: 'boolean_map',
      input: first.inputId,
      trueWhen,
    }
  } else if (form.mode === 'numeric') {
    transform = {
      kind: 'numeric',
      input: first.inputId,
      scale: finiteNumber(form.scale, 1, '倍率'),
      offset: finiteNumber(form.offset, 0, '偏移'),
      minimum: finiteNumber(form.minimum, -1000000000, '最小值'),
      maximum: finiteNumber(form.maximum, 1000000000, '最大值'),
    }
  } else if (form.mode === 'state') {
    transform = {
      kind: 'enum',
      input: first.inputId,
      entries: stateEntries(form.entries),
    }
  } else if (form.mode === 'formula') {
    const expression = String(form.expression ?? '').trim()
    if (!expression) throw new Error('请填写公式')
    transform = {
      kind: 'formula',
      expression,
      scheduleSeconds: 1,
      controlEligible: false,
    }
  } else {
    transform = {
      kind: 'passthrough',
      input: first.inputId,
    }
  }

  let control: Record<string, unknown> | undefined
  if (form.controlEnabled) {
    if (form.mode !== 'passthrough' || points.length !== 1) {
      throw new Error('允许控制只支持单个点位的直接使用')
    }
    if (first.point.read_write.toUpperCase() !== 'RW') {
      throw new Error('允许控制只能用于 RW 原始点位')
    }
    if (!['FLOAT', 'INT'].includes(outputType)) {
      throw new Error('允许控制目前只支持数值点位')
    }
    const minimum = requiredFiniteNumber(form.controlMinimum, '最小值')
    const maximum = requiredFiniteNumber(form.controlMaximum, '最大值')
    const tolerance = requiredFiniteNumber(form.controlTolerance, '回读容差')
    const cooldownSeconds = requiredFiniteNumber(form.controlCooldownSeconds, '冷却秒数')
    const timeoutSeconds = requiredFiniteNumber(form.controlTimeoutSeconds, '回读超时秒数')
    if (minimum > maximum) throw new Error('控制最小值不能大于最大值')
    if (tolerance < 0) throw new Error('控制回读容差不能小于 0')
    if (!Number.isInteger(cooldownSeconds) || cooldownSeconds < 1) {
      throw new Error('控制冷却秒数必须是正整数')
    }
    if (!Number.isInteger(timeoutSeconds) || timeoutSeconds < 1) {
      throw new Error('控制回读超时秒数必须是正整数')
    }
    control = {
      minimum,
      maximum,
      tolerance,
      cooldownSeconds,
      timeoutSeconds,
      highRisk: false,
    }
  }

  const output: Record<string, unknown> & {
    id: string
    entityDefinition: string
    transform: Record<string, unknown>
  } = {
    id: outputId,
    entityDefinition: definitionKey,
    dataType: outputType,
    unit: outputUnit || null,
    freshness: `${freshnessSeconds}s`,
    transform,
  }
  if (control) output.control = control

  return {
    content: {
      schemaVersion: 'zizu.point-processing/v1alpha1',
      id: `inline.${stableKey(definitionKey, 'entity')}`,
      kind: 'point_processing_template',
      displayName,
      deviceCategory: form.deviceCategory.toUpperCase(),
      brand: 'ZiZu',
      model: 'INLINE',
      revision: 1,
      status: 'active',
      inputs: keyedPoints.map(({ point, inputId }) => ({
        id: inputId,
        sourceKind: 'l0',
        sourceKey: point.name,
        aliases: point.display_name && point.display_name !== point.name
          ? [point.display_name]
          : [],
        dataType: point.data_type.toUpperCase(),
        unit: point.unit,
        required: true,
      })),
      outputs: [output],
    },
    inputSelections: Object.fromEntries(
      keyedPoints.map(({ point, inputId }) => [inputId, point.id]),
    ),
  }
}
