import { useCallback, useEffect, useRef, type SyntheticEvent } from 'react'
import { DecisionGraph, DecisionTable, JdmConfigProvider, ensureWasmLoaded, type DecisionGraphRef, type DecisionTableType } from '@gorules/jdm-editor'
import type { DragDropManager } from 'dnd-core'
import { DndProvider } from 'react-dnd'
import { HTML5Backend } from 'react-dnd-html5-backend'
import '../../monaco'
import '@gorules/jdm-editor/dist/style.css'

ensureWasmLoaded().catch(() => {})

// Native callbacks are debounced. A capture barrier is conservative: interactions
// that do not produce onChange remain unconfirmed, never treated as saved content.
function useNativeBarrier(onPendingChange?: (pending: boolean) => void, sourceForEvent: (event: SyntheticEvent) => string = () => 'table') {
  const pending = useRef(new Set<string>())
  const acknowledge = (source = 'table') => {
    pending.current.delete(source)
    onPendingChange?.(pending.current.size > 0)
  }
  const capture = (event: SyntheticEvent) => {
    const target = event.target instanceof Element ? event.target : null
    if (!target) return
    // React changes radio/checkbox state during click. The following native input
    // event is a duplicate notification, not another edit awaiting confirmation.
    if (event.type === 'input' && target instanceof HTMLInputElement && ['radio', 'checkbox'].includes(target.type)) return
    pending.current.add(sourceForEvent(event))
    onPendingChange?.(true)
  }
  return { acknowledge, handlers: {
    onInputCapture: capture, onChangeCapture: capture, onPasteCapture: capture, onCutCapture: capture,
    onDragStartCapture: capture, onDropCapture: capture,
    onKeyDownCapture: (event: React.KeyboardEvent) => {
      if (event.key.length === 1 || ['Backspace', 'Delete', 'Enter'].includes(event.key)) capture(event)
    },
    onClickCapture: (event: React.MouseEvent) => {
      if ((event.target as Element).closest('button, [role="menuitem"], [role="option"], input, select')) capture(event)
    },
    onPointerDownCapture: (event: React.PointerEvent) => {
      if ((event.target as Element).closest('.sort-handler.draggable, [draggable="true"], .react-flow__node, .react-flow__handle, .react-flow__edge')) capture(event)
    },
  } }
}

export function NativeDecisionGraphEditor({ graph, onChange, onPendingChange }: {
  graph: { nodes: any[]; edges: any[]; [key: string]: any }
  onChange: (graph: { nodes: any[]; edges: any[]; [key: string]: any }) => void
  onPendingChange?: (pending: boolean) => void
}) {
  const editorRef = useRef<DecisionGraphRef | null>(null)
  const lastNode = useRef<string | null>(null)
  const tabEdit = useRef<{ id: string; content: unknown } | null>(null)
  const controlledGraph = useRef(graph)
  if (!tabEdit.current) controlledGraph.current = graph
  const barrier = useNativeBarrier(onPendingChange, (event) => {
    const target = event.target as Element
    const nodeId = target.closest('.react-flow__node')?.getAttribute('data-id')
    if (nodeId) { lastNode.current = nodeId; return 'node:' + nodeId }
    const state = editorRef.current?.stateStore.getState()
    const activeNode = state?.decisionGraph.nodes.find((node) => node.id === state.activeTab)
    if (activeNode) {
      // Isolate the native tab producer until its own content commits. The graph
      // canvas/settings and controlled-value reconciliation must not acknowledge it.
      tabEdit.current ??= { id: activeNode.id, content: activeNode.content }
      return 'node:' + activeNode.id
    }
    // Native settings portals retain React capture ancestry but not DOM ancestry.
    if (!event.currentTarget.contains(target) && lastNode.current) return 'node:' + lastNode.current
    return 'graph'
  })
  const rejectConflictingInteraction = (event: SyntheticEvent) => {
    if (!tabEdit.current) return false
    const target = event.target as Element
    const nativeTab = target.closest('.tab-content.active')
    const tabPortal = !event.currentTarget.contains(target)
      && target.closest('.ant-popover, .ant-dropdown, .ant-select-dropdown, .ant-modal-root')
      && !target.closest('.settings-form')
    if (nativeTab || tabPortal) return false
    event.preventDefault()
    event.stopPropagation()
    return true
  }
  const guardedHandlers = Object.fromEntries(Object.entries(barrier.handlers).map(([name, handler]) => [
    name, (event: SyntheticEvent) => { if (!rejectConflictingInteraction(event)) handler(event as never) },
  ]))
  const callbacks = useRef({ onChange, acknowledge: barrier.acknowledge })
  callbacks.current = { onChange, acknowledge: barrier.acknowledge }
  const unsubscribe = useRef<(() => void) | undefined>(undefined)
  const graphRef = useCallback((editor: DecisionGraphRef | null) => {
    unsubscribe.current?.()
    editorRef.current = editor
    // During a tab edit, graph-side interaction is rejected and the controlled
    // value is frozen. Only that native tab may produce its content commit.
    // A same-node name/position update is explicitly NOT a content confirmation.
    unsubscribe.current = editor?.stateStore.subscribe((next, previous) => {
      if (tabEdit.current && next.activeTab !== tabEdit.current.id) {
        editor.openTab(tabEdit.current.id)
        return
      }
      if (next.decisionGraph === previous.decisionGraph) return
      callbacks.current.onChange(next.decisionGraph)
      for (const node of next.decisionGraph.nodes) {
        const before = previous.decisionGraph.nodes.find((candidate) => candidate.id === node.id)
        if (tabEdit.current?.id === node.id) {
          if (node.content !== tabEdit.current.content) {
            tabEdit.current = null
            callbacks.current.acknowledge('node:' + node.id)
          }
          continue
        }
        if (JSON.stringify(before) !== JSON.stringify(node)) callbacks.current.acknowledge('node:' + node.id)
      }
      const oldIds = previous.decisionGraph.nodes.map((node) => node.id)
      const newIds = next.decisionGraph.nodes.map((node) => node.id)
      for (const id of oldIds) if (!newIds.includes(id)) callbacks.current.acknowledge('node:' + id)
      if (JSON.stringify(oldIds) !== JSON.stringify(newIds) || JSON.stringify(previous.decisionGraph.edges) !== JSON.stringify(next.decisionGraph.edges)) callbacks.current.acknowledge('graph')
    })
  }, [])
  useEffect(() => () => unsubscribe.current?.(), [])
  return <div {...guardedHandlers} onDoubleClickCapture={rejectConflictingInteraction} onContextMenuCapture={rejectConflictingInteraction} data-testid="native-decision-graph" className="mt-4 h-[520px] overflow-hidden rounded-xl border border-white/70"><JdmConfigProvider><DndProvider backend={HTML5Backend}><DecisionGraph ref={graphRef} value={controlledGraph.current} mode="dev" /></DndProvider></JdmConfigProvider></div>
}

export default function NativeDecisionTableEditor({
  content,
  onChange,
  onPendingChange,
  manager,
  id = 'strategy-native-decision-table',
}: {
  content: unknown
  onChange: (content: unknown) => void
  onPendingChange?: (pending: boolean) => void
  manager?: DragDropManager
  id?: string
}) {
  const barrier = useNativeBarrier(onPendingChange)
  return <div {...barrier.handlers} data-native-table className="native-decision-table overflow-hidden rounded-xl border border-white/70 bg-white/50" data-testid="native-decision-table">
    <JdmConfigProvider>
      <DecisionTable
        id={id}
        tableHeight={460}
        manager={manager}
        mountDialogsOnBody
        mode="dev"
        value={content as DecisionTableType}
        onChange={(value) => { onChange(value); barrier.acknowledge() }}
      />
    </JdmConfigProvider>
  </div>
}
