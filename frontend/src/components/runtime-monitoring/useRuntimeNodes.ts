import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { EntityInstance } from '../../api/client'
import {
  connectCommittedFrameStream,
  fetchCommittedFrameSnapshot,
} from '../../api/committedFrameStream'
import {
  applyFrameDelta,
  replaceSnapshot,
  type CommittedFrameProjection,
} from '../data-trunk/committedFrameProjection'

export type RuntimeConnectionStatus = 'loading' | 'current' | 'degraded' | 'error'

export interface RuntimeNodeState {
  projection: CommittedFrameProjection | null
  status: RuntimeConnectionStatus
  error: string
}

interface NodeResource {
  generation: number
  controller: AbortController | null
  stopStream: () => void
  idleTimer: number | null
  projection: CommittedFrameProjection | null
  start: () => void
  cleanup: () => void
}

const initialNodeState = (): RuntimeNodeState => ({
  projection: null,
  status: 'loading',
  error: '',
})

export function useRuntimeNodes(descriptors: EntityInstance[]): {
  states: ReadonlyMap<string, RuntimeNodeState>
  retryNode: (nodeId: string) => void
} {
  const nodeInputs = useMemo(() => {
    const nodes = new Map<string, number>()
    for (const descriptor of descriptors) {
      const seconds = Math.max(0, descriptor.freshness_seconds)
      const current = nodes.get(descriptor.node_id)
      if (current == null || seconds < current) nodes.set(descriptor.node_id, seconds)
    }
    return [...nodes.entries()]
  }, [descriptors])
  const lifecycleKey = nodeInputs.map(([nodeId, freshness]) => `${nodeId}:${freshness}`).join('|')
  const resourcesRef = useRef(new Map<string, NodeResource>())
  const [states, setStates] = useState<Map<string, RuntimeNodeState>>(new Map())

  const patchState = useCallback((nodeId: string, patch: Partial<RuntimeNodeState>) => {
    setStates((current) => {
      const next = new Map(current)
      next.set(nodeId, { ...(current.get(nodeId) || initialNodeState()), ...patch })
      return next
    })
  }, [])

  useEffect(() => {
    let active = true
    const resources = new Map<string, NodeResource>()
    resourcesRef.current = resources
    setStates(new Map(nodeInputs.map(([nodeId]) => [nodeId, initialNodeState()])))

    for (const [nodeId, freshnessSeconds] of nodeInputs) {
      const resource: NodeResource = {
        generation: 0,
        controller: null,
        stopStream: () => {},
        idleTimer: null,
        projection: null,
        start: () => {},
        cleanup: () => {},
      }

      const clearLifecycle = () => {
        resource.controller?.abort()
        resource.controller = null
        resource.stopStream()
        resource.stopStream = () => {}
        if (resource.idleTimer !== null) window.clearTimeout(resource.idleTimer)
        resource.idleTimer = null
      }
      resource.cleanup = clearLifecycle

      const scheduleIdleRevalidation = (generation: number) => {
        if (freshnessSeconds <= 0) return
        if (resource.idleTimer !== null) window.clearTimeout(resource.idleTimer)
        resource.idleTimer = window.setTimeout(() => {
          if (active && generation === resource.generation) resource.start()
        }, freshnessSeconds * 1000)
      }

      resource.start = () => {
        const generation = ++resource.generation
        clearLifecycle()
        const controller = new AbortController()
        resource.controller = controller
        patchState(nodeId, {
          projection: resource.projection,
          status: 'loading',
          error: '',
        })

        void fetchCommittedFrameSnapshot(nodeId, controller.signal).then((snapshot) => {
          if (!active || generation !== resource.generation) return
          if (snapshot.node_id !== nodeId) throw new Error('实时快照节点身份不匹配。')
          resource.projection = replaceSnapshot(resource.projection, snapshot)
          patchState(nodeId, {
            projection: resource.projection,
            status: 'current',
            error: '',
          })
          scheduleIdleRevalidation(generation)
          resource.stopStream = connectCommittedFrameStream({
            nodeId,
            cursor: snapshot.cursor,
            onDelta: (delta) => {
              if (!active || generation !== resource.generation || !resource.projection) return
              try {
                resource.projection = applyFrameDelta(resource.projection, delta)
                patchState(nodeId, {
                  projection: resource.projection,
                  status: 'current',
                  error: '',
                })
                scheduleIdleRevalidation(generation)
              } catch {
                resource.start()
              }
            },
            onResnapshotRequired: () => resource.start(),
            onError: (reason) => {
              if (!active || generation !== resource.generation) return
              patchState(nodeId, {
                projection: resource.projection,
                status: 'degraded',
                error: reason.message,
              })
            },
          })
        }).catch((reason) => {
          if (!active || generation !== resource.generation) return
          if (reason instanceof DOMException && reason.name === 'AbortError') return
          patchState(nodeId, {
            projection: resource.projection,
            status: 'error',
            error: reason instanceof Error ? reason.message : '读取节点实时数据失败。',
          })
        })
      }

      resources.set(nodeId, resource)
      resource.start()
    }

    return () => {
      active = false
      for (const resource of resources.values()) {
        resource.generation += 1
        resource.cleanup()
      }
      if (resourcesRef.current === resources) resourcesRef.current = new Map()
    }
  // nodeInputs is deliberately represented by a primitive identity. Descriptor
  // object churn must not tear down healthy node subscriptions.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lifecycleKey, patchState])

  const retryNode = useCallback((nodeId: string) => {
    resourcesRef.current.get(nodeId)?.start()
  }, [])

  return { states, retryNode }
}
