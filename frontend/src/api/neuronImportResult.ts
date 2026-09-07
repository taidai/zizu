export class NeuronImportApiError extends Error {
  status: number
  code: string | null
  payload: unknown
  constructor(message: string, status: number, code: string | null, payload: unknown) {
    super(message)
    this.name = 'NeuronImportApiError'
    this.status = status
    this.code = code
    this.payload = payload
  }
}
export class NeuronImportResultUnknownError extends Error {
  originalCause: unknown
  constructor(cause: unknown) {
    super('导入结果尚未确认，请先核对当前点位，不要重复提交。')
    this.name = 'NeuronImportResultUnknownError'
    this.originalCause = cause
  }
}

type ImportReceipt = {
  status: string
  configuration_revision: number
  counts: Partial<Record<'create' | 'update' | 'unchanged' | 'conflict', number>>
}

export async function readNeuronImportResult(response: Response): Promise<ImportReceipt> {
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    const detail = payload?.detail
    const code = typeof detail?.code === 'string' ? detail.code : null
    const message = typeof detail === 'string' ? detail : typeof detail?.message === 'string' ? detail.message : `导入请求返回 HTTP ${response.status}`
    throw new NeuronImportApiError(message, response.status, code, payload)
  }
  try {
    const receipt = await response.json()
    if (!receipt || typeof receipt.status !== 'string'
      || !Number.isInteger(receipt.configuration_revision)
      || !receipt.counts || typeof receipt.counts !== 'object' || Array.isArray(receipt.counts)
      || Object.values(receipt.counts).some((count) => typeof count !== 'number' || !Number.isInteger(count) || count < 0)) {
      throw new Error('Incomplete import receipt')
    }
    return receipt
  } catch (cause) {
    throw new NeuronImportResultUnknownError(cause)
  }
}
