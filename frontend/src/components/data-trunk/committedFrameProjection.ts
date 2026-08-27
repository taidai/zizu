import type {
  CommittedFrameDelta,
  CommittedFrameSnapshot,
  FrameFailure,
  L0FrameItem,
  L2FrameItem,
} from '../../api/committedFrameStream'

export interface CommittedFrameProjection {
  nodeId: string
  cursor: string
  frameSequence: number
  frameTime: string | null
  configurationRevision: number
  status: 'COMPLETE' | 'FAILED' | null
  failure: FrameFailure | null
  l0: Map<string, L0FrameItem>
  l2: Map<string, L2FrameItem>
}

export function replaceSnapshot(
  _current: CommittedFrameProjection | null,
  snapshot: CommittedFrameSnapshot,
): CommittedFrameProjection {
  return {
    nodeId: snapshot.node_id,
    cursor: snapshot.cursor,
    frameSequence: snapshot.frame_sequence,
    frameTime: snapshot.frame_time,
    configurationRevision: snapshot.configuration_revision,
    status: null,
    failure: null,
    l0: new Map(snapshot.l0.map((item) => [item.tag_id, { ...item }])),
    l2: new Map(
      snapshot.l2.map((item) => [item.entity_instance_id, { ...item }]),
    ),
  }
}

export function applyFrameDelta(
  current: CommittedFrameProjection,
  delta: CommittedFrameDelta,
): CommittedFrameProjection {
  if (delta.frame_sequence <= current.frameSequence) return current
  if (delta.frame_sequence !== current.frameSequence + 1) {
    throw new Error('FRAME_SEQUENCE_GAP')
  }

  const l0 = new Map(current.l0)
  const l2 = new Map(current.l2)
  for (const item of delta.l0_changes) {
    l0.set(item.tag_id, {
      ...item,
      frame_sequence: delta.frame_sequence,
    })
  }
  for (const item of delta.l2_changes) {
    l2.set(item.entity_instance_id, {
      ...item,
      configuration_revision: delta.configuration_revision,
      frame_sequence: delta.frame_sequence,
    })
  }
  return {
    ...current,
    cursor: delta.cursor,
    frameSequence: delta.frame_sequence,
    frameTime: delta.frame_time,
    configurationRevision: delta.configuration_revision,
    status: delta.status,
    failure: delta.failure,
    l0,
    l2,
  }
}
