export type InlinePointProcessingMode = 'passthrough' | 'numeric' | 'state' | 'formula'

export interface InlineRawPoint {
  id: string
  name: string
  display_name: string | null
  data_type: string
  unit: string | null
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

function stableKey(value: string, fallback: string): string {
  const normalized = value
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
  return normalized || fallback
}

function pointInputIds(points: readonly InlineRawPoint[]): string[] {
  const used = new Set<string>()
  return points.map((point, index) => {
    const base = stableKey(point.name, `point_${index + 1}`)
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

  return {
    mode: points.length > 1 ? 'formula' : 'passthrough',
    displayName: first.display_name || first.name,
    definitionKey: `${/^[a-z]/.test(category) ? category : `device_${category}`}.${sourceKey || identityFallback}`,
    unit: first.unit || '',
    dataType: first.data_type.toUpperCase(),
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
  const outputType = form.mode === 'state'
    ? 'ENUM'
    : form.mode === 'numeric'
      ? 'FLOAT'
    : String(form.dataType || first.point.data_type).toUpperCase()
  const outputUnit = form.unit === undefined ? first.point.unit : form.unit
  if (
    form.mode === 'passthrough'
    && (outputUnit || null) !== (first.point.unit || null)
  ) {
    throw new Error('直接使用不能改变单位，请选择倍率与偏移')
  }
  let transform: Record<string, unknown>
  if (form.mode === 'numeric') {
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
  } else if (first.point.data_type.toUpperCase() === 'FLOAT') {
    transform = {
      kind: 'numeric',
      input: first.inputId,
      scale: 1,
      offset: 0,
      minimum: -1000000000,
      maximum: 1000000000,
    }
  } else {
    transform = {
      kind: 'formula',
      expression: first.inputId,
      scheduleSeconds: 1,
      controlEligible: false,
    }
  }

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
      outputs: [{
        id: outputId,
        entityDefinition: definitionKey,
        dataType: outputType,
        unit: outputUnit || null,
        freshness: `${freshnessSeconds}s`,
        transform,
      }],
    },
    inputSelections: Object.fromEntries(
      keyedPoints.map(({ point, inputId }) => [inputId, point.id]),
    ),
  }
}
