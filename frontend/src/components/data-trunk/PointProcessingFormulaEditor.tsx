import { useEffect, useState } from 'react'

import type {
  PointProcessingFormulaPreview,
  PointProcessingTemplate,
} from '../../api/client'
import {
  buildFormulaPreviewViewModel,
  buildVisualFormula,
  parseVisualFormula,
  type VisualFormulaFunction,
} from './dataTrunkViewModel'

const visualFunctions: Array<{ key: VisualFormulaFunction; label: string }> = [
  { key: 'sum', label: '求和' },
  { key: 'avg', label: '平均' },
  { key: 'min_of', label: '最小值' },
  { key: 'max_of', label: '最大值' },
  { key: 'count', label: '计数' },
]

export default function PointProcessingFormulaEditor({
  template,
  preview,
  busy,
  onPreview,
}: {
  template: PointProcessingTemplate
  preview: PointProcessingFormulaPreview | null
  busy: boolean
  onPreview: (expression: string) => void
}) {
  const output = template.outputs.find((item) => item.transform.kind === 'formula')
  const templateExpression = output?.transform.expression || ''
  const [mode, setMode] = useState<'visual' | 'text'>('visual')
  const [expression, setExpression] = useState(templateExpression)

  useEffect(() => setExpression(templateExpression), [template.revision_id, templateExpression])
  if (!output) return null
  const collectionInputs = template.inputs.filter((item) => item.cardinality === 'many')
  const visual = parseVisualFormula(
    expression,
    collectionInputs.map((item) => item.input_id),
  )
  const currentPreview = preview?.expression === expression.trim() ? preview : null
  const model = currentPreview ? buildFormulaPreviewViewModel(currentPreview) : null

  return (
    <section className="rounded-lg border border-blue-200 bg-blue-50/60 p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[11px] font-semibold text-blue-950">跨节点强类型公式</div>
          <div className="mt-1 text-[10px] text-blue-800">
            每 {output.transform.scheduleSeconds ?? 1} 秒计算一次
          </div>
        </div>
        <div className="flex rounded-md border border-blue-200 bg-white p-0.5 text-[10px]">
          {(['visual', 'text'] as const).map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => setMode(item)}
              className={`rounded px-2 py-1 ${mode === item ? 'bg-blue-700 text-white' : 'text-blue-800'}`}
            >
              {item === 'visual' ? '可视化' : '文本'}
            </button>
          ))}
        </div>
      </div>

      {mode === 'visual' ? (
        <div className="mt-3 rounded-md border border-blue-100 bg-white p-3">
          <div className="text-[10px] font-semibold text-gray-600">1. 选择集合函数</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {visualFunctions.map((item) => (
              <button
                key={item.key}
                type="button"
                disabled={!collectionInputs.length}
                onClick={() => setExpression(buildVisualFormula(
                  item.key,
                  visual?.inputId || collectionInputs[0].input_id,
                ))}
                className={`rounded border px-2 py-1 text-[10px] ${visual?.functionName === item.key ? 'border-blue-600 bg-blue-100 text-blue-900' : 'border-gray-200 text-gray-600'}`}
              >
                {item.label}
              </button>
            ))}
          </div>
          <div className="mt-3 text-[10px] font-semibold text-gray-600">2. 选择冻结输入</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {collectionInputs.map((input) => (
              <button
                key={input.input_id}
                type="button"
                onClick={() => setExpression(buildVisualFormula(
                  visual?.functionName || 'sum',
                  input.input_id,
                ))}
                className={`rounded border px-2 py-1 text-[10px] ${visual?.inputId === input.input_id ? 'border-blue-600 bg-blue-100 text-blue-900' : 'border-gray-200 text-gray-700'}`}
              >
                {input.source_key} · {input.data_type}{input.unit ? `/${input.unit}` : ''}
              </button>
            ))}
          </div>
          {!visual && (
            <div className="mt-3 rounded bg-amber-50 px-2 py-1.5 text-[10px] text-amber-800">
              当前是复杂文本公式；选择上方函数和输入可重建为可视公式。
            </div>
          )}
          <code className="mt-3 block break-all font-mono text-[11px] text-gray-600">{expression}</code>
        </div>
      ) : (
        <textarea
          aria-label="公式表达式"
          value={expression}
          onChange={(event) => setExpression(event.target.value)}
          rows={3}
          className="neu-input mt-3 w-full resize-y bg-white px-3 py-2 font-mono text-xs"
        />
      )}

      <div className="mt-2 text-[10px] leading-4 text-blue-800">
        检查只验证草稿，不改变已发布模板；正式发布仍使用所选模板修订中的公式。
      </div>
      <button
        type="button"
        disabled={busy || !expression.trim()}
        onClick={() => onPreview(expression)}
        className="neu-btn mt-2 w-full px-3 py-2 text-xs font-semibold text-blue-700 disabled:opacity-50"
      >
        {busy ? '正在检查公式...' : '检查公式、成员与依赖'}
      </button>

      {model && currentPreview && (
        <div className={`mt-3 grid gap-2 text-[10px] sm:grid-cols-3 ${model.ready ? 'text-emerald-800' : 'text-red-700'}`}>
          <div className="rounded border border-current/20 bg-white p-2"><div className="text-gray-500">结果契约</div><div className="mt-1 font-semibold">{model.resultContract}</div></div>
          <div className="rounded border border-current/20 bg-white p-2"><div className="text-gray-500">选择器</div><div className="mt-1 font-semibold">{model.memberLabel}</div></div>
          <div className="rounded border border-current/20 bg-white p-2"><div className="text-gray-500">全站 DAG</div><div className="mt-1 font-semibold">{model.dagLabel}</div></div>
          <details className="sm:col-span-3 rounded border border-gray-200 bg-white p-2 text-gray-500">
            <summary className="cursor-pointer font-medium">技术详情</summary>
            <div className="mt-2 break-all">输出 {output.entity_definition_id} · AST {currentPreview.ast_digest.slice(0, 16)} · DAG {currentPreview.dag_summary.digest?.slice(0, 16) || '阻断'}</div>
          </details>
        </div>
      )}
    </section>
  )
}
