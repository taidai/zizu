import { DecisionGraph, DecisionTable, JdmConfigProvider, ensureWasmLoaded, type DecisionTableType } from '@gorules/jdm-editor'
import { DndProvider } from 'react-dnd'
import { HTML5Backend } from 'react-dnd-html5-backend'
import '../../monaco'
import '@gorules/jdm-editor/dist/style.css'

ensureWasmLoaded().catch(() => {})

export function NativeDecisionGraphEditor({ graph, onChange }: {
  graph: { nodes: any[]; edges: any[]; [key: string]: any }
  onChange: (graph: { nodes: any[]; edges: any[]; [key: string]: any }) => void
}) {
  return <div className="mt-4 h-[520px] overflow-hidden rounded-xl border border-white/70"><JdmConfigProvider><DndProvider backend={HTML5Backend}><DecisionGraph value={graph} onChange={onChange} mode="dev" /></DndProvider></JdmConfigProvider></div>
}

export default function NativeDecisionTableEditor({
  content,
  onChange,
  onPendingChange,
}: {
  content: unknown
  onChange: (content: unknown) => void
  onPendingChange?: (pending: boolean) => void
}) {
  return <div onInputCapture={(event) => { if (event.target instanceof HTMLElement && event.target.isContentEditable) onPendingChange?.(true) }} className="native-decision-table overflow-hidden rounded-xl border border-white/70 bg-white/50" data-testid="native-decision-table">
    <p className="p-3 text-xs text-gray-600">添加条件列并填写输入别名或原生公式；单元格填写条件，规则行可新增、删除。action_id 填写第 3 步输出别名（加双引号，如 "fan_enable"）；target 填写强类型目标值（如 true 或 12.5）。新表使用 collect 与输出路径 intents，多条命中按行序产生意图；动态目标仍受后端发布安全校验。试算零设备写入，已有规则图不会自动转换。</p>
    <JdmConfigProvider>
      <DecisionTable
        id="strategy-native-decision-table"
        tableHeight={460}
        mountDialogsOnBody
        mode="dev"
        value={content as DecisionTableType}
        onChange={(value) => { onChange(value); onPendingChange?.(false) }}
      />
    </JdmConfigProvider>
  </div>
}
