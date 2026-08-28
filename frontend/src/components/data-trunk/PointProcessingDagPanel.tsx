import type { PointProcessingPlan } from '../../api/client'

export default function PointProcessingDagPanel({ plan }: { plan: PointProcessingPlan }) {
  const selectorItems = plan.items.filter((item) => item.kind === 'selector_binding')
  const dag = plan.items.find((item) => item.kind === 'dag_validation')
  if (!selectorItems.length && !dag) return null

  return (
    <details className="mt-3 rounded-lg border border-gray-200 bg-gray-50 p-3 text-[10px]">
      <summary className="cursor-pointer font-semibold text-gray-700">技术详情：冻结成员与依赖图</summary>
      <section className="mt-3">
      <div className="flex items-center justify-between gap-3">
        <span className="font-semibold text-gray-800">冻结成员与全站依赖</span>
        <span className={dag?.action === 'block' ? 'text-red-700' : 'text-emerald-700'}>
          {dag?.action === 'block' ? 'DAG 阻断' : 'DAG 已通过'}
        </span>
      </div>
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        {selectorItems.map((item) => (
          <div key={item.item_key} className="rounded border border-gray-200 bg-white p-2">
            <div className="font-medium text-gray-700">{item.input_id}</div>
            <div className="mt-1 text-gray-500">{item.selected_source_ids?.length || 0} 个实体 · {item.cardinality === 'many' ? '多实体' : '单实体'}</div>
            <div className="mt-1 truncate font-mono text-gray-400">{item.selector_digest}</div>
          </div>
        ))}
        {dag && (
          <div className="rounded border border-gray-200 bg-white p-2">
            <div className="font-medium text-gray-700">依赖图</div>
            <div className="mt-1 text-gray-500">{dag.planned_edges?.length || 0} 条新依赖 · 深度 {dag.max_depth ?? '—'}/8</div>
            <div className="mt-1 truncate font-mono text-gray-400">{dag.dag_digest}</div>
          </div>
        )}
      </div>
      </section>
    </details>
  )
}
