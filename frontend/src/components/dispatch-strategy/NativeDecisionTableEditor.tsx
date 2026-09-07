import { DecisionTable, JdmConfigProvider, type DecisionTableType } from '@gorules/jdm-editor'

export default function NativeDecisionTableEditor({
  content,
  onChange,
}: {
  content: unknown
  onChange: (content: unknown) => void
}) {
  return <div className="native-decision-table overflow-hidden rounded-xl border border-white/70 bg-white/50" data-testid="native-decision-table">
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
