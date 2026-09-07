import { DecisionTable, JdmConfigProvider, type DecisionTableType } from '@gorules/jdm-editor'

export default function NativeDecisionTableEditor({
  content,
  onChange,
}: {
  content: unknown
  onChange: (content: unknown) => void
}) {
  return <div className="native-decision-table overflow-hidden rounded-xl border border-white/70 bg-white/50" data-testid="native-decision-table">
    <p className="p-3 text-xs text-gray-600">在 action_id 填写已绑定的输出别名（字符串需加双引号，如 "fan_enable"）；target 填写目标值（如 true 或 12.5）。新表使用 collect 与输出路径 intents，多条命中规则按表中从上到下的顺序产生控制意图；试算不会下发设备。已有规则图不会自动转换。</p>
    <JdmConfigProvider>
      <DecisionTable
        id="strategy-native-decision-table"
        tableHeight={460}
        mountDialogsOnBody
        mode="dev"
        value={content as DecisionTableType}
        onChange={(value) => onChange(value)}
      />
    </JdmConfigProvider>
  </div>
}
