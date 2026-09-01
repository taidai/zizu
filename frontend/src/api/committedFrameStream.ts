import { apiFetch } from './client'

export type FrameValue = number | boolean | string | string[] | null

export interface L0FrameItem {
  tag_id: string
  node_id: string
  name?: string
  display_name?: string
  data_type: string
  value: FrameValue
  unit: string | null
  source_quality: number
  effective_quality: number
  source_timestamp: string | null
  received_at: string | null
  accepted_beat: number | null
  source_path: string | null
  source_type: string | null
  source_digest?: string | null
  reason?: string | null
  frame_sequence: number
}

export interface L2FrameItem {
  entity_instance_id: string
  node_id: string
  definition_id?: string
  display_name?: string
  data_type: string
  value: FrameValue
  unit: string | null
  quality: number
  reason: string | null
  observed_at: string | null
  value_observed_at: string | null
  received_at: string | null
  calculated_at: string | null
  processing_revision_id: string | null
  configuration_revision?: number | null
  source_digest: string | null
  frame_sequence: number
}

export interface FrameFailure {
  failure_id?: string | null
  code: string | null
}

export interface CommittedFrameSnapshot {
  type: 'frame_snapshot'
  node_id: string
  cursor: string
  frame_sequence: number
  frame_time: string | null
  configuration_revision: number
  frame_status: 'COMPLETE' | 'FAILED' | null
  failure: FrameFailure | null
  backlog_frames: number
  l0: L0FrameItem[]
  l2: L2FrameItem[]
}

export interface CommittedFrameDelta {
  type: 'frame_delta'
  cursor: string
  frame_id: string
  frame_sequence: number
  status: 'COMPLETE' | 'FAILED'
  frame_time: string
  configuration_revision: number
  l0_changes: Omit<L0FrameItem, 'frame_sequence'>[]
  l2_changes: Omit<L2FrameItem, 'frame_sequence'>[]
  failure: FrameFailure | null
}

export async function fetchCommittedFrameSnapshot(
  nodeId: string,
  signal?: AbortSignal,
): Promise<CommittedFrameSnapshot> {
  const response = await apiFetch(
    `/api/v1/runtime/frame-snapshot?node_id=${encodeURIComponent(nodeId)}`,
    { signal },
  )
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as {
      detail?: { message?: string }
    } | null
    throw new Error(payload?.detail?.message || `读取实时数据失败：${response.status}`)
  }
  return response.json() as Promise<CommittedFrameSnapshot>
}

export interface CommittedFrameStreamOptions {
  nodeId: string
  cursor: string
  onDelta: (delta: CommittedFrameDelta) => void
  onResnapshotRequired: (code: string) => void
  onError?: (error: Error) => void
}

export function connectCommittedFrameStream(
  options: CommittedFrameStreamOptions,
): () => void {
  let socket: WebSocket | null = null
  let reconnectTimer: number | null = null
  let cancelled = false
  let generation = 0
  let attempt = 0
  let cursor = options.cursor
  let resnapshotRequested = false

  const requestResnapshot = (code: string) => {
    if (resnapshotRequested || cancelled) return
    resnapshotRequested = true
    options.onResnapshotRequired(code)
    socket?.close()
  }

  const report = (error: unknown) => {
    options.onError?.(
      error instanceof Error ? error : new Error('实时数据连接失败'),
    )
  }

  const scheduleReconnect = () => {
    if (cancelled || resnapshotRequested || reconnectTimer !== null) return
    attempt += 1
    const delay = Math.min(5000, attempt * 1000)
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      void connect()
    }, delay)
  }

  const connect = async () => {
    if (cancelled || resnapshotRequested) return
    const currentGeneration = ++generation
    try {
      const issued = await apiFetch('/api/v1/auth/ws-ticket', { method: 'POST' })
      if (!issued.ok) throw new Error(`实时票据获取失败：${issued.status}`)
      const { ticket } = await issued.json() as { ticket: string }
      if (cancelled || currentGeneration !== generation) return

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      socket = new WebSocket(
        `${protocol}//${window.location.host}/api/v1/ws/data-frames`,
      )
      socket.onopen = () => {
        if (currentGeneration !== generation) return
        socket?.send(JSON.stringify({ authenticate: { ticket } }))
      }
      socket.onmessage = (event) => {
        if (currentGeneration !== generation || cancelled) return
        try {
          const payload = JSON.parse(event.data) as {
            type?: string
            code?: string
          }
          if (payload.type === 'authenticated') {
            socket?.send(JSON.stringify({
              subscribe: { node_id: options.nodeId, after: cursor },
            }))
          } else if (payload.type === 'subscribed') {
            attempt = 0
          } else if (payload.type === 'frame_delta') {
            const delta = payload as CommittedFrameDelta
            options.onDelta(delta)
            cursor = delta.cursor
          } else if (payload.type === 'resnapshot_required') {
            requestResnapshot(payload.code || 'FRAME_RESNAPSHOT_REQUIRED')
          }
        } catch (error) {
          report(error)
          requestResnapshot('FRAME_PAYLOAD_INVALID')
        }
      }
      socket.onerror = () => report(new Error('实时数据连接异常'))
      socket.onclose = (event) => {
        if (currentGeneration !== generation) return
        socket = null
        if (event.code === 4401 || event.code === 4403) {
          report(new Error('实时数据身份已失效'))
          return
        }
        scheduleReconnect()
      }
    } catch (error) {
      if (currentGeneration !== generation || cancelled) return
      report(error)
      scheduleReconnect()
    }
  }

  void connect()
  return () => {
    cancelled = true
    generation += 1
    if (reconnectTimer !== null) window.clearTimeout(reconnectTimer)
    reconnectTimer = null
    socket?.close()
    socket = null
  }
}
