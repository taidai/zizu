import fs from 'node:fs'
import path from 'node:path'

export function summarizeAcceptanceRun({ startedAt, endedAt, results }) {
  const counts = { passed: 0, failed: 0, skipped: 0, total: results.length }
  for (const result of results) {
    const key = result.status === 'passed' ? 'passed' : result.status === 'skipped' ? 'skipped' : 'failed'
    counts[key] += 1
  }
  return {
    counts,
    durationMs: Math.max(0, endedAt - startedAt),
    failures: results
      .filter((result) => result.status !== 'passed' && result.status !== 'skipped')
      .map((result) => ({
        title: result.title,
        error: result.error || 'unknown failure',
        artifacts: [...(result.artifacts || [])],
      })),
    results,
  }
}

export function redactSecrets(value, secrets) {
  const normalizedSecrets = secrets.filter((secret) => typeof secret === 'string' && secret.length > 0)
  if (typeof value === 'string') {
    return normalizedSecrets.reduce(
      (current, secret) => current.split(secret).join('[REDACTED]'),
      value,
    )
  }
  if (Array.isArray(value)) {
    return value.map((item) => redactSecrets(item, normalizedSecrets))
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, redactSecrets(item, normalizedSecrets)]),
    )
  }
  return value
}

export default class NodeManagementReporter {
  constructor(options = {}) {
    this.outputFile = options.outputFile || 'test-results/node-management-summary.json'
    this.startedAt = 0
    this.results = []
  }

  onBegin() {
    this.startedAt = Date.now()
  }

  onTestEnd(test, result) {
    this.results.push({
      title: test.titlePath().join(' › '),
      status: result.status,
      durationMs: result.duration,
      error: result.error?.message || '',
      artifacts: result.attachments.map((attachment) => attachment.path).filter(Boolean),
    })
  }

  onEnd() {
    const summary = summarizeAcceptanceRun({
      startedAt: this.startedAt,
      endedAt: Date.now(),
      results: this.results,
    })
    const safeSummary = redactSecrets(summary, [
      process.env.ZIZU_E2E_USERNAME,
      process.env.ZIZU_E2E_PASSWORD,
    ])
    const outputPath = path.resolve(this.outputFile)
    fs.mkdirSync(path.dirname(outputPath), { recursive: true })
    fs.writeFileSync(outputPath, `${JSON.stringify(safeSummary, null, 2)}\n`, 'utf8')
    process.stdout.write(
      `\n节点管理验收：${safeSummary.counts.passed} 通过 / ${safeSummary.counts.failed} 失败 / ` +
        `${safeSummary.counts.skipped} 跳过，耗时 ${(safeSummary.durationMs / 1000).toFixed(1)} 秒\n`,
    )
    if (safeSummary.failures.length > 0) {
      process.stdout.write(`失败证据已写入 ${outputPath}\n`)
    }
  }
}
