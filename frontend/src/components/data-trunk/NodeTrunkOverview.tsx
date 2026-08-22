import type {
  EntityInstance,
  EntityInstanceObservation,
  NodeDataTrunk,
  PointProcessingTemplate,
} from '../../api/client'
import EntityObservationCard from './EntityObservationCard'

function sourceDisplayName(sourceKey: string): string {
  const labels: Record<string, string> = {
    ActivePowerRaw: '原始有功功率',
    PActKw: '原始有功功率',
    RunningState: '运行状态码',
    ModeCode: '运行状态码',
    FaultCodeText: '原始故障码',
    AlarmList: '原始故障码',
  }
  return labels[sourceKey] || sourceKey
}

export default function NodeTrunkOverview({
  trunk,
  installedTemplate,
  descriptors,
  observations,
  histories,
  readOnly,
}: {
  trunk: NodeDataTrunk
  installedTemplate: PointProcessingTemplate | null
  descriptors: Map<string, EntityInstance>
  observations: Map<string, EntityInstanceObservation>
  histories: Map<string, EntityInstanceObservation[]>
  readOnly: boolean
}) {
  const entityRows = trunk.l2
    .map((item) => ({ ...item, descriptor: descriptors.get(item.entity_instance_id) }))
    .filter((item): item is typeof item & { descriptor: EntityInstance } => Boolean(item.descriptor))

  return (
    <section className={`grid gap-3 ${readOnly ? 'grid-cols-1' : 'xl:grid-cols-[0.8fr_0.9fr_1.7fr]'}`}>
      {!readOnly && (
        <div className="rounded-xl border border-gray-200 bg-white/45 p-3">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-semibold text-gray-800">L0 原始点位</h3>
            <span className="text-[10px] text-gray-500">{trunk.l0.length} 个</span>
          </div>
          <p className="mt-1 text-[10px] leading-4 text-gray-500">协议采集的原始值，只作为转换输入，不供上层业务直接引用。</p>
          <div className="mt-3 space-y-2">
            {trunk.l0.map((source) => (
              <div key={source.source_id} className="border-l-2 border-gray-300 pl-2">
                <div className="text-xs font-medium text-gray-700">{sourceDisplayName(source.source_key)}</div>
                <div className="mt-0.5 text-[10px] text-gray-500">{source.source_key}　{source.data_type}{source.unit ? `　${source.unit}` : ''}</div>
              </div>
            ))}
            {trunk.l0.length === 0 && <p className="rounded-lg bg-amber-50 p-2 text-[10px] text-amber-700">该节点还没有可匹配的原始点位。</p>}
          </div>
        </div>
      )}

      {!readOnly && (
        <div className="rounded-xl border border-blue-200 bg-blue-50/55 p-3">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-semibold text-blue-950">L1 点位加工</h3>
            <span className={`rounded-md px-2 py-0.5 text-[10px] font-semibold ${trunk.l1_summary.installed ? 'bg-green-100 text-green-700' : 'bg-gray-200 text-gray-600'}`}>
              {trunk.l1_summary.installed ? '已安装' : '未安装'}
            </span>
          </div>
          <p className="mt-1 text-[10px] leading-4 text-blue-900/65">完成品牌字段映射、单位换算、状态解析、故障码解析和质量判定。</p>
          <div className="mt-4 border-t border-blue-200 pt-3">
            <div className="text-sm font-semibold text-gray-800">{installedTemplate?.display_name || '等待选择转换模板'}</div>
            {installedTemplate && (
              <dl className="mt-2 grid grid-cols-2 gap-2 text-[10px] text-gray-600">
                <div><dt>品牌型号</dt><dd className="mt-0.5 font-medium text-gray-800">{installedTemplate.brand} {installedTemplate.model}</dd></div>
                <div><dt>模板版本</dt><dd className="mt-0.5 font-medium text-gray-800">修订 {installedTemplate.revision}</dd></div>
                <div><dt>输入</dt><dd className="mt-0.5 font-medium text-gray-800">{installedTemplate.inputs.length} 个</dd></div>
                <div><dt>输出</dt><dd className="mt-0.5 font-medium text-gray-800">{installedTemplate.outputs.length} 个</dd></div>
              </dl>
            )}
          </div>
        </div>
      )}

      <div className="rounded-xl border border-gray-200 bg-white/45 p-3">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-xs font-semibold text-gray-800">L2 全局实体</h3>
            <p className="mt-1 text-[10px] text-gray-500">告警、策略、控制和画面只使用这里的实时值、质量、时间戳和来源证据。</p>
          </div>
          <span className="text-[10px] text-gray-500">{entityRows.length} 个</span>
        </div>
        <div className="mt-3 grid grid-cols-1 gap-3 2xl:grid-cols-2">
          {entityRows.map((item) => (
            <EntityObservationCard
              key={item.entity_instance_id}
              descriptor={item.descriptor}
              observation={observations.get(item.entity_instance_id) || null}
              history={histories.get(item.entity_instance_id) || []}
            />
          ))}
          {entityRows.length === 0 && (
            <div className="rounded-lg border border-dashed border-gray-300 p-5 text-center text-xs text-gray-500">
              当前节点尚未形成 L2 全局实体。请先安装解决方案并完成点位加工。
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
