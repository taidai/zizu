import { useEffect, useState } from 'react'

import type {
  PointProcessingFormulaPreview,
  PointProcessingTemplate,
} from '../../api/client'
import { buildFormulaPreviewViewModel } from './dataTrunkViewModel'

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
  const model = preview ? buildFormulaPreviewViewModel(preview) : null

  return (
    <section className="rounded-lg border border-blue-200 bg-blue-50/60 p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-[11px] font-semibold text-blue-950">跨节点强类型公式</div>
          <div className="mt-1 text-[10px] text-blue-800">
            {output.entity_definition_id} · 每 {output.transform.scheduleSeconds ?? 1} 秒计算
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
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <span className="rounded bg-blue-100 px-2 py-1 font-semibold text-blue-900">集合求和</span>
            <span className="text-gray-400">←</span>
            {template.inputs.filter((item) => expression.includes(item.input_id)).map((input) => (
              <span key={input.input_id} className="rounded border border-gray-200 px-2 py-1 text-gray-700">
                {input.input_id} · {input.data_type}{input.unit ? `/${input.unit}` : ''}
              </span>
            ))}
            <span className="text-gray-400">→</span>
            <span className="rounded bg-emerald-50 px-2 py-1 font-semibold text-emerald-800">
              {output.data_type}{output.unit ? `/${output.unit}` : ''}
            </span>
          </div>
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
        预检只验证草稿，不改变已签名模板；正式应用仍使用所选模板修订中的公式。
      </div>
      <button
        type="button"
        disabled={busy || !expression.trim()}
        onClick={() => onPreview(expression)}
        className="neu-btn mt-2 w-full px-3 py-2 text-xs font-semibold text-blue-700 disabled:opacity-50"
      >
        {busy ? '正在编译并展开实体...' : '预检公式、成员与 DAG'}
      </button>

      {model && preview && (
        <div className={`mt-3 grid gap-2 text-[10px] sm:grid-cols-3 ${model.ready ? 'text-emerald-800' : 'text-red-700'}`}>
          <div className="rounded border border-current/20 bg-white p-2"><div className="text-gray-500">结果契约</div><div className="mt-1 font-semibold">{model.resultContract}</div></div>
          <div className="rounded border border-current/20 bg-white p-2"><div className="text-gray-500">选择器</div><div className="mt-1 font-semibold">{model.memberLabel}</div></div>
          <div className="rounded border border-current/20 bg-white p-2"><div className="text-gray-500">全站 DAG</div><div className="mt-1 font-semibold">{model.dagLabel}</div></div>
          <div className="sm:col-span-3 break-all text-gray-500">AST {preview.ast_digest.slice(0, 16)} · DAG {preview.dag_summary.digest?.slice(0, 16) || '阻断'}</div>
        </div>
      )}
    </section>
  )
}
