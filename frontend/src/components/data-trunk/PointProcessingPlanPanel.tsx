import type {
  NodeDataTrunk,
  PointProcessingPlan,
  PointProcessingTemplate,
} from '../../api/client'
import { buildDataTrunkViewModel, planActionLabel } from './dataTrunkViewModel'

function inputName(inputId: string): string {
  return {
    active_power_raw: '有功功率',
    operating_state_raw: '运行状态',
    fault_codes_raw: '故障码',
  }[inputId] || inputId
}

export default function PointProcessingPlanPanel({
  trunk,
  templates,
  selectedTemplate,
  selections,
  plan,
  busy,
  resultUnknown,
  onTemplateChange,
  onSelectionChange,
  onPlan,
  onApply,
}: {
  trunk: NodeDataTrunk
  templates: PointProcessingTemplate[]
  selectedTemplate: PointProcessingTemplate | null
  selections: Record<string, string>
  plan: PointProcessingPlan | null
  busy: 'plan' | 'apply' | 'acceptance' | null
  resultUnknown: boolean
  onTemplateChange: (revisionId: string) => void
  onSelectionChange: (inputId: string, sourceId: string) => void
  onPlan: () => void
  onApply: () => void
}) {
  const model = buildDataTrunkViewModel({ plan })
  const scanDriven = selectedTemplate?.asset_id === 'pcs.en9'
  const isSwap = Boolean(
    trunk.l1_summary.revision_id
    && selectedTemplate
    && trunk.l1_summary.revision_id !== selectedTemplate.revision_id,
  )

  return (
    <aside className="rounded-xl border border-gray-200 bg-white/55 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">匹配与应用</h3>
          <p className="mt-1 text-[10px] leading-4 text-gray-500">先生成只读计划，处理全部阻断后再原子应用。</p>
        </div>
        {plan && (
          <span className={`rounded-md px-2 py-1 text-[10px] font-semibold ${plan.status === 'ready' ? 'bg-green-100 text-green-700' : plan.status === 'blocked' ? 'bg-red-100 text-red-700' : 'bg-blue-100 text-blue-700'}`}>
            {plan.status === 'ready' ? '可应用' : plan.status === 'blocked' ? '有阻断' : '已应用'}
          </span>
        )}
      </div>

      <label className="mt-4 block text-[11px] font-medium text-gray-700">
        点位加工模板
        <select
          value={selectedTemplate?.revision_id || ''}
          onChange={(event) => onTemplateChange(event.target.value)}
          className="neu-input mt-1.5 w-full bg-transparent px-3 py-2 text-xs"
        >
          <option value="">请选择品牌型号</option>
          {templates.map((template) => (
            <option key={template.revision_id} value={template.revision_id}>
              {template.display_name}（修订 {template.revision}）
            </option>
          ))}
        </select>
      </label>

      {selectedTemplate && (
        <div className="mt-4 space-y-3">
          {scanDriven && (
            <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 text-[11px] leading-5 text-blue-900">
              <div className="font-semibold">Neuron 只读扫描</div>
              <div>系统将核对 {selectedTemplate.inputs.length} 个 L0 点位，生成 L1 加工和 {selectedTemplate.outputs.length} 个 L2 全局实体；不会改写驱动、点位或设备参数。</div>
            </div>
          )}
          {!scanDriven && selectedTemplate.inputs.map((input) => (
            <label key={input.input_id} className="block text-[11px] font-medium text-gray-700">
              {inputName(input.input_id)}{input.required ? '（必需）' : ''}
              <select
                value={selections[input.input_id] || ''}
                onChange={(event) => onSelectionChange(input.input_id, event.target.value)}
                className="neu-input mt-1.5 w-full bg-transparent px-3 py-2 text-xs"
              >
                <option value="">自动匹配</option>
                {trunk.l0.map((source) => (
                  <option key={source.source_id} value={source.source_id}>
                    {source.source_key}{source.unit ? `（${source.unit}）` : ''}
                  </option>
                ))}
              </select>
            </label>
          ))}
          <button
            type="button"
            onClick={onPlan}
            disabled={busy !== null}
            className="neu-btn w-full px-3 py-2 text-xs font-semibold text-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy === 'plan' ? '正在只读扫描并生成计划...' : scanDriven ? '扫描并生成统一计划' : '生成匹配计划'}
          </button>
        </div>
      )}

      {isSwap && (
        <div className="mt-4 rounded-lg border border-blue-200 bg-blue-50 p-3 text-[10px] text-blue-900">
          <div className="font-semibold">换牌升级保持业务身份</div>
          <div className="mt-1 grid gap-1 sm:grid-cols-3 xl:grid-cols-1 2xl:grid-cols-3">
            <span>输入绑定重新匹配</span><span>L2 实体标识保持</span><span>告警、策略、画面引用保持</span>
          </div>
        </div>
      )}

      {plan && (
        <div className="mt-4 border-t border-gray-200 pt-4">
          <div className="grid grid-cols-5 gap-1 text-center text-[10px]">
            {Object.entries(model.counts).map(([action, count]) => (
              <div key={action} className="rounded-md bg-gray-100 px-1 py-2 text-gray-600">
                <div className="font-mono-value text-sm font-semibold text-gray-900">{count}</div>
                <div className="mt-0.5">{action === 'delete_candidate' ? '停用' : planActionLabel(action as keyof typeof model.counts)}</div>
              </div>
            ))}
          </div>
          <div className="mt-3 grid grid-cols-3 gap-2 text-center text-[10px]">
            {model.layers.map((layer) => (
              <div key={layer} className="rounded-lg border border-gray-200 bg-white px-2 py-2">
                <div className="font-mono-value text-base font-semibold text-gray-900">{model.layerCounts[layer]}</div>
                <div className="mt-0.5 text-gray-500">{layer === 'L0' ? '原始点位' : layer === 'L1' ? '点位加工' : '全局实体'}</div>
              </div>
            ))}
          </div>
          <div className={`mt-3 rounded-lg p-3 text-xs ${model.canApply ? 'bg-green-50 text-green-800' : 'bg-amber-50 text-amber-800'}`}>
            <span className="font-semibold">下一步：</span>{model.nextAction}
          </div>
          {plan.items.length > 0 && (
            <div className="mt-3 max-h-56 space-y-3 overflow-y-auto text-[10px] text-gray-600">
              {model.layers.map((layer) => {
                const items = plan.items.filter((item) => item.layer === layer)
                if (!items.length) return null
                return (
                  <div key={layer}>
                    <div className="sticky top-0 bg-white/95 py-1 font-semibold text-gray-800">{layer} · {items.length} 项</div>
                    {items.map((item) => (
                      <div key={item.item_key} className="flex justify-between gap-2 border-b border-gray-200 py-1 last:border-b-0">
                        <span className="truncate">{inputName(item.input_id || item.output_id || item.item_key)}</span>
                        <span className="shrink-0 font-medium">{planActionLabel(item.action)}</span>
                      </div>
                    ))}
                  </div>
                )
              })}
            </div>
          )}
          {resultUnknown && (
            <div role="alert" className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-[10px] leading-4 text-amber-800">
              上次应用结果未知。系统已保留原请求标识，请点击同一按钮查询或完成同一笔应用，不要重新生成计划。
            </div>
          )}
          <button
            type="button"
            onClick={onApply}
            disabled={!model.canApply || busy !== null}
            className="mt-3 w-full rounded-lg bg-blue-700 px-3 py-2 text-xs font-semibold text-white shadow-sm transition active:translate-y-px disabled:cursor-not-allowed disabled:bg-gray-300 disabled:text-gray-600"
          >
            {busy === 'apply' ? '正在应用...' : resultUnknown ? '使用原请求重试' : '应用点位加工'}
          </button>
        </div>
      )}

      {!selectedTemplate && templates.length === 0 && (
        <div className="mt-4 rounded-lg border border-dashed border-gray-300 p-4 text-center text-xs text-gray-500">
          当前解决方案没有适用于该设备类型的点位加工模板。
        </div>
      )}
    </aside>
  )
}
