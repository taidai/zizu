const REQUIRED_VARIABLES = [
  'ZIZU_E2E_BASE_URL',
  'ZIZU_E2E_USERNAME',
  'ZIZU_E2E_PASSWORD',
  'ZIZU_E2E_ALLOW_LIVE_WRITES',
  'ZIZU_E2E_WRITE_ROOT',
]

const REQUIRED_WRITE_ROOT = 'E2E验证'
const SAFE_RESOURCE_LABEL = /^[\p{L}\p{N}_-]+$/u

export function buildAcceptanceEnvironment(source = process.env) {
  for (const key of REQUIRED_VARIABLES) {
    if (!String(source[key] ?? '').trim()) {
      throw new Error(`缺少必需环境变量 ${key}`)
    }
  }
  if (source.ZIZU_E2E_ALLOW_LIVE_WRITES !== '1') {
    throw new Error('ZIZU_E2E_ALLOW_LIVE_WRITES 必须明确设为 1')
  }
  if (source.ZIZU_E2E_WRITE_ROOT !== REQUIRED_WRITE_ROOT) {
    throw new Error(`测试写入根节点必须严格等于 ${REQUIRED_WRITE_ROOT}`)
  }

  const parsedUrl = new URL(source.ZIZU_E2E_BASE_URL)
  if (!['http:', 'https:'].includes(parsedUrl.protocol)) {
    throw new Error('ZIZU_E2E_BASE_URL 只允许 http 或 https')
  }

  const runId = String(source.ZIZU_E2E_RUN_ID ?? defaultRunId()).trim()
  if (!SAFE_RESOURCE_LABEL.test(runId)) {
    throw new Error('ZIZU_E2E_RUN_ID 只能包含文字、数字、下划线或连字符')
  }

  return Object.freeze({
    baseUrl: parsedUrl.toString().replace(/\/$/, ''),
    username: source.ZIZU_E2E_USERNAME,
    password: source.ZIZU_E2E_PASSWORD,
    writeRoot: REQUIRED_WRITE_ROOT,
    runId,
  })
}

export function buildTemporaryResourceName(environment, label) {
  const normalizedLabel = String(label ?? '').trim()
  if (!SAFE_RESOURCE_LABEL.test(normalizedLabel)) {
    throw new Error('资源标签只能包含文字、数字、下划线或连字符')
  }
  return `${environment.writeRoot}-${normalizedLabel}-${environment.runId}`
}

export function printableAcceptanceSummary(environment) {
  return Object.freeze({
    baseUrl: environment.baseUrl,
    writeRoot: environment.writeRoot,
    runId: environment.runId,
  })
}

function defaultRunId() {
  return new Date().toISOString().replace(/[-:.]/g, '').replace(/\.\d{3}Z$/, 'Z')
}
