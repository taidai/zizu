import type {
  NodeDataTrunk,
  PointProcessingFormulaPreview,
  PointProcessingPlan,
  PointProcessingTemplate,
} from '../../api/client'
import {
  buildDataTrunkViewModel,
  manualBindableInputs,
  planActionLabel,
  POINT_PROCESSING_ACTIONS,
  pointCandidateLabel,
  scannedInputCandidates,
} from './dataTrunkViewModel'
import PointProcessingDagPanel from './PointProcessingDagPanel'
import PointProcessingFormulaEditor from './PointProcessingFormulaEditor'

function inputName(inputId: string): string {
  return {
    active_power_raw: '有功功率',
    operating_state_raw: '运行状态',
    fault_codes_raw: '故障码',
  }[inputId] || inputId
}

const panelClass = 'rounded-lg border border-gray-200 bg-white p-4'

export default function PointProcessingPlanPanel({
  trunk,
  templates,
  selectedTemplate,
  recommendedRevisionId,
  selections,
  plan,
  busy,
  resultUnknown,
  onTemplateChange,
  onSelectionChange,
  onPlan,
  onApply,
  formulaPreview,
  onFormulaPreview,
}: {
  trunk: NodeDataTrunk
  templates: PointProcessingTemplate[]
  selectedTemplate: PointProcessingTemplate | null
  recommendedRevisionId: string
  selections: Record<string, string>
  plan: PointProcessingPlan | null
  busy: 'plan' | 'apply' | 'acceptance' | 'formula' | null
  resultUnknown: boolean
  onTemplateChange: (revisionId: string) => void
  onSelectionChange: (inputId: string, sourceId: string) => void
  onPlan: () => void
  onApply: () => void
  formulaPreview: PointProcessingFormulaPreview | null
  onFormulaPreview: (expression: string) => void
}) {
  const model = buildDataTrunkViewModel({ plan })
  const scanDriven = selectedTemplate?.requires_scan ?? false
  const isSwap = Boolean(
    trunk.l1_summary.revision_id
    && selectedTemplate
    && trunk.l1_summary.revision_id !== selectedTemplate.revision_id,
  )
  const directInputs = manualBindableInputs(selectedTemplate?.inputs || [])
  const selectorInputs = selectedTemplate?.inputs.filter((input) => input.selector) || []
  const planItemName = (item: PointProcessingPlan['items'][number]): string => {
    if (item.input_id) {
      const input = selectedTemplate?.inputs.find((candidate) => candidate.input_id === item.input_id)
      return inputName(item.input_id) !== item.input_id
        ? inputName(item.input_id)
        : input?.source_key || '输入点位'
    }
    if (item.output_id) {
      const index = selectedTemplate?.outputs.findIndex((output) => output.output_key === item.output_id) ?? -1
      return index >= 0 ? `输出实体 ${index + 1}` : '输出实体'
    }
    return item.kind === 'dag_validation' ? '全站依赖检查' : '加工变更'
  }

  return (
    <section className="rounded-xl border border-gray-200 bg-white/55 p-4" aria-label="点位加工工作区">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">定义点位加工</h3>
          <p className="mt-1 text-[11px] leading-4 text-gray-500">选择输入、核对加工规则、确认输出，然后一次发布。</p>
        </div>
        {plan && (
          <span className={`rounded px-2 py-1 text-[10px] font-semibold ${
            plan.status === 'ready'
              ? 'bg-green-100 text-green-700'
              : plan.status === 'blocked'
                ? 'bg-red-100 text-red-700'
                : 'bg-blue-100 text-blue-700'
          }`}>
            {plan.status === 'ready' ? '检查通过' : plan.status === 'blocked' ? '需要处理' : '已发布'}
          </span>
        )}
      </div>

      <div className="mt-4 grid gap-3 xl:grid-cols-3">
        <div className={panelClass} aria-label="输入点位">
          <h4 className="text-xs font-semibold text-gray-900">1. 输入点位</h4>
          <label className="mt-3 block text-[11px] font-medium text-gray-700">
            点位加工模板
            <select
              value={selectedTemplate?.revision_id || ''}
              onChange={(event) => onTemplateChange(event.target.value)}
              className="neu-input mt-1.5 w-full bg-transparent px-3 py-2 text-xs"
            >
              <option value="">请选择品牌和型号</option>
              {templates.map((template) => (
                <option key={template.revision_id} value={template.revision_id}>
                  {template.brand} / {template.model} / 修订 {template.revision}
                  {template.revision_id === recommendedRevisionId ? '（推荐）' : ''}
                </option>
              ))}
            </select>
          </label>

          {selectedTemplate && scanDriven && (
            <div className="mt-3 rounded border border-blue-200 bg-blue-50 p-3 text-[11px] leading-5 text-blue-900">
              <div className="font-semibold">自动核对 Neuron 点位</div>
              <div>只读检查 {selectedTemplate.inputs.length} 个输入，不改写驱动、点位或设备参数。</div>
            </div>
          )}

          {selectedTemplate && directInputs.map((input) => {
            const sourceMap = new Map([
              ...trunk.l0,
              ...scannedInputCandidates(plan?.items || [], input.input_id),
            ].map((source) => [source.source_id, source]))
            const compatibleSources = [...sourceMap.values()].filter((source) => (
              source.data_type === input.data_type
              && (source.unit || null) === (input.unit || null)
            ))
            return (
              <label key={input.input_id} className="mt-3 block text-[11px] font-medium text-gray-700">
                {inputName(input.input_id)}{input.required ? '（必需）' : ''}
                <select
                  value={selections[input.input_id] || ''}
                  onChange={(event) => onSelectionChange(input.input_id, event.target.value)}
                  className="neu-input mt-1.5 w-full bg-transparent px-3 py-2 text-xs"
                >
                  <option value="">自动匹配</option>
                  {compatibleSources.map((source) => (
                    <option key={source.source_id} value={source.source_id}>
                      {pointCandidateLabel(source)}
                    </option>
                  ))}
                </select>
              </label>
            )
          })}

          {selectedTemplate && selectorInputs.map((input) => (
            <div key={input.input_id} className="mt-3 rounded border border-gray-200 bg-gray-50 p-3 text-[10px]">
              <div className="font-semibold text-gray-800">{inputName(input.input_id)}，自动选择</div>
              <div className="mt-1 text-gray-500">
                发布时固定当前后代 {input.selector?.nodeType} 的 {input.selector?.entityDefinition} 实体清单。
              </div>
            </div>
          ))}

          {!selectedTemplate && templates.length === 0 && (
            <div className="mt-3 rounded border border-dashed border-gray-300 p-4 text-center text-xs text-gray-500">
              当前设备类型没有可用的点位加工模板。
            </div>
          )}
        </div>

        <div className={panelClass} aria-label="加工规则">
          <h4 className="text-xs font-semibold text-gray-900">2. 加工规则</h4>
          {selectedTemplate ? (
            <>
              <dl className="mt-3 grid grid-cols-2 gap-2 text-[11px]">
                <div><dt className="text-gray-400">模板</dt><dd className="mt-1 font-medium text-gray-800">{selectedTemplate.display_name}</dd></div>
                <div><dt className="text-gray-400">修订</dt><dd className="mt-1 font-medium text-gray-800">{selectedTemplate.revision}</dd></div>
                <div><dt className="text-gray-400">输入</dt><dd className="mt-1 font-medium text-gray-800">{selectedTemplate.inputs.length} 个</dd></div>
                <div><dt className="text-gray-400">输出</dt><dd className="mt-1 font-medium text-gray-800">{selectedTemplate.outputs.length} 个实体</dd></div>
              </dl>
              <div className="mt-3">
                <PointProcessingFormulaEditor
                  template={selectedTemplate}
                  preview={formulaPreview}
                  busy={busy === 'formula'}
                  onPreview={onFormulaPreview}
                />
              </div>
              <button
                type="button"
                onClick={onPlan}
                disabled={busy !== null}
                className="neu-btn mt-4 w-full px-3 py-2 text-xs font-semibold text-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy === 'plan' ? POINT_PROCESSING_ACTIONS.inspecting : POINT_PROCESSING_ACTIONS.inspect}
              </button>
            </>
          ) : (
            <p className="mt-3 text-xs text-gray-500">先在左侧选择模板。</p>
          )}
        </div>

        <div className={panelClass} aria-label="输出预览">
          <h4 className="text-xs font-semibold text-gray-900">3. 输出预览</h4>
          {!plan && <p className="mt-3 text-xs text-gray-500">点击“检查加工结果”后，这里会列出变更、阻断和稳定实体。</p>}

          {isSwap && (
            <div className="mt-3 rounded border border-blue-200 bg-blue-50 p-3 text-[10px] leading-5 text-blue-900">
              更换品牌只重新匹配输入，实体身份以及告警、策略、画面的引用保持不变。
            </div>
          )}

          {plan && (
            <>
              <div className="mt-3 grid grid-cols-5 gap-1 text-center text-[10px]">
                {Object.entries(model.counts).map(([action, count]) => (
                  <div key={action} className="rounded bg-gray-100 px-1 py-2 text-gray-600">
                    <div className="font-mono-value text-sm font-semibold text-gray-900">{count}</div>
                    <div className="mt-0.5">{action === 'delete_candidate' ? '停用' : planActionLabel(action as keyof typeof model.counts)}</div>
                  </div>
                ))}
              </div>
              <div className={`mt-3 rounded p-3 text-xs ${model.canApply ? 'bg-green-50 text-green-800' : 'bg-amber-50 text-amber-800'}`}>
                <span className="font-semibold">下一步：</span>{model.nextAction}
              </div>
              {plan.items.length > 0 && (
                <div className="mt-3 max-h-48 overflow-y-auto text-[10px] text-gray-600">
                  {plan.items.map((item) => (
                    <div key={item.item_key} className="flex justify-between gap-2 py-1">
                      <span className="truncate">{planItemName(item)}</span>
                      <span className="shrink-0 font-medium">{planActionLabel(item.action)}</span>
                    </div>
                  ))}
                </div>
              )}
              <PointProcessingDagPanel plan={plan} />
              {resultUnknown && (
                <div role="alert" className="mt-3 rounded border border-amber-300 bg-amber-50 p-3 text-[10px] leading-4 text-amber-800">
                  上次发布结果未知。系统已保留原请求，请使用同一个按钮继续，不要重新检查。
                </div>
              )}
              <button
                type="button"
                onClick={onApply}
                disabled={!model.canApply || busy !== null}
                className="mt-3 w-full rounded bg-blue-700 px-3 py-2 text-xs font-semibold text-white disabled:cursor-not-allowed disabled:bg-gray-300 disabled:text-gray-600"
              >
                {busy === 'apply'
                  ? POINT_PROCESSING_ACTIONS.publishing
                  : resultUnknown
                    ? '继续上次发布'
                    : POINT_PROCESSING_ACTIONS.publish}
              </button>
            </>
          )}
        </div>
      </div>
    </section>
  )
}
