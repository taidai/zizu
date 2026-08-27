import type {
  EntityInstance,
  EntityInstanceObservation,
  NodeDataTrunk,
  PointProcessingTemplate,
} from '../../api/client'
import EntityObservationCard from './EntityObservationCard'
import type { CommittedFrameProjection } from './committedFrameProjection'

const QUALITY_LABEL: Record<number, string> = {
  192: '正常', 64: '不确定', 1: '陈旧', 0: '无效',
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (Array.isArray(value)) return value.join('、')
  return typeof value === 'number' ? value.toFixed(3).replace(/\.?0+$/, '') : String(value)
}

function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString('zh-CN') : '未记录'
}

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
  projection,
  histories,
  readOnly,
}: {
  trunk: NodeDataTrunk
  installedTemplate: PointProcessingTemplate | null
  descriptors: Map<string, EntityInstance>
  projection: CommittedFrameProjection | null
  histories: Map<string, EntityInstanceObservation[]>
  readOnly: boolean
}) {
  const entityRows = trunk.l2
    .map((item) => ({ ...item, descriptor: descriptors.get(item.entity_instance_id) }))
    .filter((item): item is typeof item & { descriptor: EntityInstance } => Boolean(item.descriptor))

  return (
    <section className="grid gap-3 xl:grid-cols-[0.8fr_0.9fr_1.7fr]">
      {(
        <div className="rounded-xl border border-gray-200 bg-white/45 p-3">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-semibold text-gray-800">L0 原始点位</h3>
          <span className="text-[10px] text-gray-500">{trunk.l0.length} 个 · 帧 {projection?.frameSequence ?? '—'}</span>
          </div>
          <p className="mt-1 text-[10px] leading-4 text-gray-500">协议采集的原始值，只作为转换输入，不供上层业务直接引用。</p>
          <div className="mt-3 max-h-[34rem] space-y-2 overflow-y-auto pr-1">
            {trunk.l0.map((source) => {
              const realtime = projection?.l0.get(source.source_id)
              const stale = (realtime?.effective_quality ?? 1) === 1
              return (
                <div key={source.source_id} className={`border-l-2 pl-2 ${stale ? 'border-gray-300 opacity-65' : 'border-green-400'}`}>
                  <div className="flex items-center justify-between gap-2">
                    <div className="truncate text-xs font-medium text-gray-700">{sourceDisplayName(source.source_key)}</div>
                    <div className="font-mono-value text-sm font-semibold text-gray-900">{formatValue(realtime?.value)}{realtime?.unit ? ` ${realtime.unit}` : ''}</div>
                  </div>
                  <div className="mt-0.5 text-[10px] text-gray-500">{source.source_key}　{source.data_type}{source.unit ? `　${source.unit}` : ''}</div>
                  <div className="mt-0.5 text-[10px] text-gray-500">质量 {QUALITY_LABEL[realtime?.effective_quality ?? 1] || realtime?.effective_quality} · 数据 {formatTime(realtime?.source_timestamp)} · 接收 {formatTime(realtime?.received_at)}</div>
                  <div className="mt-0.5 truncate text-[10px] text-gray-400" title={realtime?.source_path || undefined}>来源 {realtime?.source_type || '—'} / {realtime?.source_path || '未记录'}</div>
                </div>
              )
            })}
            {trunk.l0.length === 0 && <p className="rounded-lg bg-amber-50 p-2 text-[10px] text-amber-700">该节点还没有可匹配的原始点位。</p>}
          </div>
        </div>
      )}

      {(
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
          <span className="text-[10px] text-gray-500">{entityRows.length} 个 · {formatTime(projection?.frameTime)} · 配置 {projection?.configurationRevision ?? '—'}</span>
        </div>
        <div className="mt-3 grid grid-cols-1 gap-3 2xl:grid-cols-2">
          {entityRows.map((item) => (
            <EntityObservationCard
              key={item.entity_instance_id}
              descriptor={item.descriptor}
              observation={projection?.l2.get(item.entity_instance_id) || null}
              history={histories.get(item.entity_instance_id) || []}
              frameTime={projection?.frameTime || null}
            />
          ))}
          {entityRows.length === 0 && (
            <div className="rounded-lg border border-dashed border-gray-300 p-5 text-center text-xs text-gray-500">
              当前节点尚未形成 L2 全局实体。请先选择点位加工模板并发布。
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
