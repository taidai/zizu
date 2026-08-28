export type TemplateDocument = Record<string, unknown> & {
  id: string
  displayName: string
  deviceCategory: string
  brand: string
  model: string
  revision: number
  inputs: Array<Record<string, unknown> & { id: string }>
  outputs: Array<Record<string, unknown> & {
    id: string
    transform: Record<string, unknown>
  }>
}

export type TemplateCopyMode = 'next-revision' | 'new-template'
export type VisualTransformKind = 'passthrough' | 'numeric' | 'enum' | 'formula'

function deepCopy<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

export function cloneTemplateDraft(
  source: TemplateDocument,
  mode: TemplateCopyMode,
): TemplateDocument {
  const draft = deepCopy(source)
  draft.status = 'active'
  if (mode === 'new-template') {
    draft.id = `${source.id}.copy`
    draft.displayName = `${source.displayName} 副本`
    draft.revision = 1
  } else {
    draft.revision = source.revision + 1
  }
  return draft
}

function finiteNumber(value: unknown, label: string): number {
  const parsed = typeof value === 'number' ? value : Number(String(value ?? '').trim())
  if (!Number.isFinite(parsed)) throw new Error(`${label}必须是数字`)
  return parsed
}

export function parseEnumEntries(value: unknown): Record<string, string> {
  const entries: Record<string, string> = {}
  for (const rawLine of String(value ?? '').split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line) continue
    const separator = line.indexOf('=')
    if (separator < 1 || separator === line.length - 1) {
      throw new Error(`枚举行格式错误：${line}`)
    }
    const source = line.slice(0, separator).trim()
    const target = line.slice(separator + 1).trim()
    if (!source || !target) throw new Error(`枚举行格式错误：${line}`)
    entries[source] = target
  }
  if (Object.keys(entries).length === 0) throw new Error('枚举映射不能为空')
  return entries
}

export function formatEnumEntries(entries: unknown): string {
  if (!entries || typeof entries !== 'object' || Array.isArray(entries)) return ''
  return Object.entries(entries as Record<string, unknown>)
    .map(([source, target]) => `${source}=${String(target)}`)
    .join('\n')
}

export function visualTransformKind(transform: Record<string, unknown>): VisualTransformKind | 'preserve' {
  if (transform.kind === 'numeric') {
    return transform.scale === 1
      && transform.offset === 0
      && transform.minimum === -1000000000
      && transform.maximum === 1000000000
      ? 'passthrough'
      : 'numeric'
  }
  if (transform.kind === 'enum') return 'enum'
  if (transform.kind === 'formula') return 'formula'
  return 'preserve'
}

export function buildTransform(
  kind: VisualTransformKind,
  input: string,
  options: {
    scale?: unknown
    offset?: unknown
    minimum?: unknown
    maximum?: unknown
    entries?: unknown
    expression?: unknown
  } = {},
): Record<string, unknown> {
  if (kind === 'passthrough') {
    return {
      kind: 'numeric', input, scale: 1, offset: 0,
      minimum: -1000000000, maximum: 1000000000,
    }
  }
  if (kind === 'numeric') {
    return {
      kind: 'numeric',
      input,
      scale: finiteNumber(options.scale ?? 1, '倍率'),
      offset: finiteNumber(options.offset ?? 0, '偏移'),
      minimum: finiteNumber(options.minimum ?? -1000000000, '最小值'),
      maximum: finiteNumber(options.maximum ?? 1000000000, '最大值'),
    }
  }
  if (kind === 'enum') {
    return { kind: 'enum', input, entries: parseEnumEntries(options.entries) }
  }
  const expression = String(options.expression ?? '').trim()
  if (!expression) throw new Error('公式不能为空')
  return {
    kind: 'formula', expression,
    scheduleSeconds: 1,
    controlEligible: false,
  }
}
