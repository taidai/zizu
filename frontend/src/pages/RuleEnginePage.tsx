import { useEffect, useMemo, useState } from 'react'
import { DecisionGraph, GraphSimulator, JdmConfigProvider } from '@gorules/jdm-editor'
import { DndProvider } from 'react-dnd'
import { HTML5Backend } from 'react-dnd-html5-backend'
import {
  fetchRules, fetchEntityInstances, createRule, updateRule, deleteRule, simulateRule, evaluateGraph, submitControlCommand, fetchRuleTemplates,
  type Rule, type RuleCreateRequest, type EntityInstance, type RuleTemplate,
} from '../api/client'

type DecisionGraphType = {
  nodes: any[]
  edges: any[]
}

type ControlAction = {
  id?: string
  type: 'control'
  entity_instance_id?: string
  value: any
}

type OutputBinding = {
  field: string
  name?: string
  entity_instance_id?: string
}

type RuleConfig = {
  sourceEntityInstanceIds: string[]
  legacySourceEntityIds: string[]
  legacyControlActions: boolean
  actions: ControlAction[]
  inputMappings?: Record<string, string>
  outputBindings?: OutputBinding[]
  template?: string
}

function extractConfig(content: any): RuleConfig {
  const configuredActions = Array.isArray(content?._config?.actions) ? content._config.actions : []
  const graphActions = Array.isArray(content?.actions) ? content.actions : []
  const legacyPhysicalFields = new Set([
    'node', 'group', 'tag', 'topic', 'payload', 'command',
    'entity_id', 'entity', 'entity_name', 'cooldown',
  ])
  const legacyControlActions = [...configuredActions, ...graphActions]
    .some((action: any) => action && typeof action === 'object' && (
      action.type === 'neuron_write'
      || (action.type === 'control' && Object.keys(action).some((key) => legacyPhysicalFields.has(key)))
    ))
  if (content && typeof content === 'object' && content._config) {
    const cfg = content._config
    return {
      sourceEntityInstanceIds: cfg.sourceEntityInstanceIds || [],
      legacySourceEntityIds: cfg.sourceEntityIds || [],
      legacyControlActions,
      actions: (cfg.actions || [])
        .filter((a: any) => a?.type === 'control')
        .map((a: any) => ({
          id: a.id || '',
          type: 'control',
          entity_instance_id: a.entity_instance_id || '',
          value: a.value ?? '',
        })),
      inputMappings: cfg.inputMappings || {},
      outputBindings: (cfg.outputBindings || []).map((b: any) => ({
        field: b.field || '',
        name: b.name || '',
        entity_instance_id: b.entity_instance_id || '',
      })),
      template: cfg.template || 'custom',
    }
  }
  return {
    sourceEntityInstanceIds: [],
    legacySourceEntityIds: [],
    legacyControlActions,
    actions: [],
    inputMappings: {},
    outputBindings: [],
    template: 'custom',
  }
}

function emptyGraph(): DecisionGraphType {
  return {
    nodes: [
      { id: 'input-1', type: 'inputNode', name: 'Request', position: { x: 70, y: 250 } },
      { id: 'output-1', type: 'outputNode', name: 'Response', position: { x: 670, y: 250 } },
    ],
    edges: [],
  }
}

function ensureGraph(content: any): DecisionGraphType {
  if (content && typeof content === 'object') {
    if (Array.isArray(content.nodes)) {
      const graph = { ...content } as DecisionGraphType
      graph.nodes = graph.nodes.map((node) => {
        const n = { ...node }
        if (n.type === 'startNode') n.type = 'inputNode'
        if (n.type === 'endNode') n.type = 'outputNode'
        if (n.type === 'decisionNode') n.type = 'decisionTableNode'
        return n
      })
      return graph
    }
    if (Array.isArray(content.inputs) && Array.isArray(content.outputs) && Array.isArray(content.rules)) {
      return {
        nodes: [
          { id: 'input-1', type: 'inputNode', name: 'Request', position: { x: 70, y: 250 } },
          { id: 'table-1', type: 'decisionTableNode', name: 'Decision Table', position: { x: 370, y: 250 }, content },
          { id: 'output-1', type: 'outputNode', name: 'Response', position: { x: 670, y: 250 } },
        ],
        edges: [
          { id: 'e1', sourceId: 'input-1', targetId: 'table-1', type: 'edge' },
          { id: 'e2', sourceId: 'table-1', targetId: 'output-1', type: 'edge' },
        ],
      }
    }
  }
  return emptyGraph()
}

function extractGraphFields(graph: DecisionGraphType) {
  const table = graph.nodes.find((n: any) => n.type === 'decisionTableNode')
  const content = table?.content || {}
  const inputs = (content.inputs || []).map((i: any) => ({ id: i.field || i.id, name: i.name || i.field || i.id }))
  const outputs = (content.outputs || []).map((o: any) => ({ id: o.field || o.id, name: o.name || o.field || o.id }))
  return { inputs, outputs }
}

function bindingsToActions(bindings: OutputBinding[]): ControlAction[] {
  return bindings
    .filter((b) => b.entity_instance_id)
    .map((b) => ({
      id: `output:${b.field}`,
      type: 'control',
      entity_instance_id: b.entity_instance_id,
      value: `{{${b.field}}}`,
    }))
}

const RULE_TYPES: RuleCreateRequest['rule_type'][] = ['control', 'linkage']

const TYPE_LABELS: Record<RuleCreateRequest['rule_type'], string> = {
  alarm: '告警 alarm',
  control: '控制 control',
  fault_map: '故障映射 fault_map',
  linkage: '联动 linkage',
}


function RuleForm({
  initial,
  templates,
  onSave,
  onCancel,
}: {
  initial?: Rule
  templates: RuleTemplate[]
  onSave: (data: RuleCreateRequest) => Promise<void>
  onCancel: () => void
}) {
  const isCreating = !initial
  const initialGraph = ensureGraph(initial?.jdm_content)
  const initialConfig = extractConfig(initial?.jdm_content)

  const [name, setName] = useState(initial?.name || '')
  const [ruleType, setRuleType] = useState<RuleCreateRequest['rule_type']>(initial?.rule_type || 'control')
  const [enabled, setEnabled] = useState(initial?.enabled ?? true)
  const [graph, setGraph] = useState<DecisionGraphType>(initialGraph)
  const [config, setConfig] = useState<RuleConfig>(initialConfig)
  const [entitySearch, setEntitySearch] = useState('')
  const [entityInstanceOptions, setEntityInstanceOptions] = useState<EntityInstance[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [simulate, setSimulate] = useState<any>()
  const [simLoading, setSimLoading] = useState(false)
  const [activeTab, setActiveTab] = useState<'input' | 'process' | 'output'>('input')
  const [advancedMode, setAdvancedMode] = useState(false)
  const [showRawActions, setShowRawActions] = useState(false)

  useEffect(() => {
    const load = async () => {
      try {
        const instances = await fetchEntityInstances()
        setEntityInstanceOptions(instances.items)
      } catch {
        setEntityInstanceOptions([])
      }
    }
    load()
  }, [])

  const graphFields = useMemo(() => extractGraphFields(graph), [graph])
  const visibleEntityInstances = useMemo(() => {
    const needle = entitySearch.trim().toLocaleLowerCase()
    if (!needle) return entityInstanceOptions
    return entityInstanceOptions.filter((item) =>
      [item.node_display_name, item.node_type, item.display_name, item.definition_id]
        .some((value) => value.toLocaleLowerCase().includes(needle)),
    )
  }, [entityInstanceOptions, entitySearch])

  useEffect(() => {
    setConfig((prev) => {
      const newInputMappings: Record<string, string> = {}
      graphFields.inputs.forEach((i: any) => {
        newInputMappings[i.id] = prev.inputMappings?.[i.id] || ''
      })
      const existingBindings = new Map((prev.outputBindings || []).map((b: any) => [b.field, b]))
      const newBindings: OutputBinding[] = graphFields.outputs
        .filter((o: any) => o.id !== 'strategy')
        .map((o: any) => existingBindings.get(o.id) || {
          field: o.id,
          name: o.name,
          entity_instance_id: '',
        })
      return { ...prev, inputMappings: newInputMappings, outputBindings: newBindings }
    })
  }, [graphFields.inputs.length, graphFields.outputs.length])

  const applyTemplate = (templateId: string) => {
    const tmpl = templates.find((t) => t.id === templateId)
    if (!tmpl) return
    setGraph(ensureGraph(tmpl.graph))
    setConfig(extractConfig({ _config: tmpl.config }))
    setRuleType(tmpl.rule_type)
  }

  const panels = useMemo(
    () => [
      {
        id: 'simulator',
        title: 'Simulator',
        icon: <span className="text-xs">▶</span>,
        hideHeader: true,
        renderPanel: () => (
          <GraphSimulator
            defaultRequest={JSON.stringify(
              { pv_power: 120, load_power: 80, soc: 45, tou_price: 0.35 },
              null,
              2,
            )}
            loading={simLoading}
            onClear={() => setSimulate(undefined)}
            onRun={async ({ graph: g, context }) => {
              setSimLoading(true)
              try {
                const data = await evaluateGraph(g, context as Record<string, any>)
                setSimulate({
                  result: {
                    performance: data.evaluation?.performance || '',
                    result: data.evaluation?.result ?? null,
                    snapshot: g,
                    trace: data.evaluation?.trace ?? {},
                  },
                })
              } catch (e: any) {
                setSimulate({
                  error: {
                    title: 'Evaluation failed',
                    message: e.message || 'Unknown error',
                    data: {},
                  },
                })
              } finally {
                setSimLoading(false)
              }
            }}
          />
        ),
      },
    ],
    [simLoading],
  )

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    if (!graph.nodes.length) {
      setError('规则图至少包含一个节点')
      return
    }
    if (config.legacySourceEntityIds.length > 0) {
      setError('该规则仍引用旧全局实体，请按迁移预览改选明确的设备实体实例后再保存')
      return
    }
    if (config.legacyControlActions) {
      setError('该规则含旧物理控制动作，需重新配置为“实体实例 + 值”的统一控制命令后才能保存')
      return
    }
    setSaving(true)
    try {
      const derivedActions = showRawActions ? config.actions : bindingsToActions(config.outputBindings || [])
      const jdm_content = {
        ...graph,
        _config: {
          sourceEntityInstanceIds: config.sourceEntityInstanceIds,
          actions: derivedActions,
          inputMappings: config.inputMappings,
          outputBindings: config.outputBindings,
          template: config.template,
        },
      }
      await onSave({ name, rule_type: ruleType, enabled, jdm_content: jdm_content as Record<string, any> })
      onCancel()
    } catch (e: any) {
      setError(e.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const updateInputMapping = (field: string, tagName: string) => {
    setConfig((prev) => ({
      ...prev,
      inputMappings: { ...(prev.inputMappings || {}), [field]: tagName },
    }))
  }

  const updateOutputBinding = (idx: number, patch: Partial<OutputBinding>) => {
    setConfig((prev) => {
      const bindings = [...(prev.outputBindings || [])]
      bindings[idx] = { ...bindings[idx], ...patch }
      return { ...prev, outputBindings: bindings }
    })
  }

  const testWrite = async (action: ControlAction) => {
    try {
      const v = action.value
      if (typeof v === 'string' && v.includes('{{')) {
        if (!confirm('当前值为模板，测试下发会写入字面量，确定继续？')) return
      }
      if (!action.entity_instance_id) throw new Error('请选择已确认的实体实例')
      const command = await submitControlCommand(action.entity_instance_id, action.value)
      alert(`命令已创建：${command.status}。现场成功需等待回读确认。`)
    } catch (e: any) {
      alert(`下发失败: ${e.message || e}`)
    }
  }

  const TabButton = ({ id, label }: { id: typeof activeTab; label: string }) => (
    <button
      type="button"
      onClick={() => setActiveTab(id)}
      className={`px-4 py-2 text-xs font-medium border-b-2 transition-colors ${
        activeTab === id
          ? 'border-[#52c41a] text-[#52c41a]'
          : 'border-transparent text-gray-500 hover:text-gray-700'
      }`}
    >
      {label}
    </button>
  )

  return (
    <div className="fixed inset-0 z-50 bg-[#f0f0f0] flex flex-col">
      <form onSubmit={handleSubmit} className="flex-1 flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-gray-200/50 bg-white shadow-sm">
          <h3 className="text-sm font-bold text-gray-800">
            {initial ? '编辑规则' : '新建规则'}
          </h3>
          <button type="button" onClick={onCancel} className="text-gray-400 hover:text-gray-600 text-lg leading-none">
            ×
          </button>
        </div>

        {/* Basic info */}
        <div className="px-5 py-3 grid grid-cols-12 gap-3 items-end bg-white shadow-sm">
          <div className="col-span-4">
            <label className="block text-xs text-gray-600 mb-1">规则名称</label>
            <input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="neu-input w-full px-3 py-1.5 text-xs"
              placeholder="例如：光储充调度策略"
            />
          </div>
          <div className="col-span-2">
            <label className="block text-xs text-gray-600 mb-1">规则类型</label>
            <select
              value={ruleType}
              onChange={(e) => setRuleType(e.target.value as RuleCreateRequest['rule_type'])}
              className="neu-input w-full px-3 py-1.5 text-xs bg-transparent"
            >
              {RULE_TYPES.map((t) => (
                <option key={t} value={t}>{TYPE_LABELS[t]}</option>
              ))}
            </select>
          </div>
          <div className="col-span-2 flex items-center pb-1.5">
            <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
                className="w-4 h-4 accent-[#52c41a]"
              />
              启用
            </label>
          </div>
          {isCreating && templates.length > 0 && (
            <div className="col-span-4">
              <label className="block text-xs text-gray-600 mb-1">规则模板</label>
              <select
                value={config.template || ''}
                onChange={(e) => applyTemplate(e.target.value)}
                className="neu-input w-full px-3 py-1.5 text-xs bg-transparent"
              >
                <option value="">-- 选择模板 --</option>
                {templates.map((t) => (
                  <option key={t.id} value={t.id}>{t.name}</option>
                ))}
              </select>
            </div>
          )}
        </div>

        {/* Tabs + error */}
        <div className="px-5 border-b border-gray-200/50 bg-white shadow-sm flex items-center justify-between">
          <div className="flex">
            <TabButton id="input" label="输入：数据源与字段映射" />
            <TabButton id="process" label="处理：决策图" />
            <TabButton id="output" label="输出：控制绑定" />
          </div>
          {error && <div className="text-xs text-red-500 pr-2">{error}</div>}
        </div>

        {/* Footer actions */}
        <div className="px-5 py-3 border-t border-gray-200/50 bg-white shadow-sm flex justify-end gap-2">
          <button type="button" onClick={onCancel} className="neu-btn px-4 py-1.5 text-xs text-gray-600">
            取消
          </button>
          <button
            type="submit"
            disabled={saving}
            className="neu-btn px-5 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50"
          >
            {saving ? '保存中...' : '保存规则'}
          </button>
        </div>

        {/* Tab content */}
        <div className="flex-1 min-h-0 p-5 overflow-hidden">
          {activeTab === 'input' && (
            <div className="flex flex-col gap-4 h-full">
              <div className="neu-card p-4 bg-white">
                <h4 className="text-sm font-bold text-gray-800 mb-2">数据源实体实例</h4>
                <p className="text-xs text-gray-500 mb-3">按设备选择稳定实体实例；同类多设备不会混用数据。</p>
                {config.legacySourceEntityIds.length > 0 && (
                  <div className="mb-3 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                    该规则仍有 {config.legacySourceEntityIds.length} 个旧全局实体引用，当前只读兼容；请重新选择实体实例后保存。
                  </div>
                )}
                {config.legacyControlActions && (
                  <div className="mb-3 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                    该规则含旧物理控制动作，当前只读兼容；请在输出页重新绑定设备实体实例后保存。
                  </div>
                )}
                <input
                  value={entitySearch}
                  onChange={(e) => setEntitySearch(e.target.value)}
                  placeholder="搜索实体..."
                  className="neu-inset w-full px-3 py-1.5 text-xs mb-2"
                />
                <div className="neu-inset p-3 max-h-[240px] overflow-y-auto">
                  {visibleEntityInstances.length === 0 && <p className="text-xs text-gray-400">暂无匹配的已确认实体实例</p>}
                  <div className="space-y-1">
                    {visibleEntityInstances.map((e) => {
                      const checked = config.sourceEntityInstanceIds.includes(e.id)
                      return (
                        <label key={e.id} className="flex items-center gap-2 py-1 hover:bg-white/40 rounded pr-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => {
                              const next = new Set(config.sourceEntityInstanceIds)
                              if (next.has(e.id)) next.delete(e.id)
                              else next.add(e.id)
                              setConfig({ ...config, sourceEntityInstanceIds: Array.from(next), legacySourceEntityIds: [] })
                            }}
                            className="w-4 h-4 accent-[#52c41a]"
                          />
                          <span className="truncate whitespace-nowrap text-gray-700 text-xs" title={e.definition_id}>{e.node_display_name} / {e.display_name}</span>
                          <span className="text-[10px] text-gray-400 font-mono ml-auto">{e.node_type}</span>
                        </label>
                      )
                    })}
                  </div>
                </div>
              </div>

              <div className="neu-card p-4 bg-white flex-1 overflow-hidden flex flex-col">
                <h4 className="text-sm font-bold text-gray-800 mb-2">字段映射</h4>
                <p className="text-xs text-gray-500 mb-3">把决策表字段名映射到设备实体实例；不映射则按字段名直接匹配。</p>
                <div className="neu-inset flex-1 overflow-auto p-3 text-xs">
                  {graphFields.inputs.length === 0 ? (
                    <div className="text-gray-400">当前决策表没有输入字段</div>
                  ) : (
                    <table className="w-full">
                      <thead className="text-[10px] text-gray-500 border-b border-gray-200">
                        <tr>
                          <th className="text-left py-2 font-medium">决策表字段</th>
                          <th className="text-left py-2 font-medium">绑定实体</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-100">
                        {graphFields.inputs.map((field: any) => {
                          const selectedEntityName = config.inputMappings?.[field.id] || ''
                          return (
                            <tr key={field.id}>
                              <td className="py-2">
                                <span className="font-medium text-gray-700">{field.name}</span>
                                <span className="text-[10px] text-gray-400 ml-2">({field.id})</span>
                              </td>
                              <td className="py-2">
                                <select
                                  value={selectedEntityName}
                                  onChange={(e) => updateInputMapping(field.id, e.target.value)}
                                  className="neu-input w-full px-2 py-1 text-xs bg-transparent"
                                >
                                  <option value="">-- 选择实体 --</option>
                                  {entityInstanceOptions.map((e) => (
                                    <option key={e.id} value={e.id}>
                                      {e.node_display_name} / {e.display_name}
                                    </option>
                                  ))}
                                </select>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  )}
                  {entityInstanceOptions.length === 0 && config.sourceEntityInstanceIds.length > 0 && (
                    <div className="mt-3 text-[10px] text-gray-400">正在加载实体列表…</div>
                  )}
                </div>
              </div>
            </div>
          )}

{activeTab === 'process' && (
            <div className="flex flex-col gap-4 h-full">
              <div className="neu-card p-4 bg-white">
                <div className="flex items-start justify-between">
                  <div>
                    <h4 className="text-sm font-bold text-gray-800">
                      {templates.find((t) => t.id === config.template)?.name || '自定义规则'}
                    </h4>
                    <p className="text-xs text-gray-500 mt-1">
                      {templates.find((t) => t.id === config.template)?.description || '自定义决策图'}
                    </p>
                  </div>
                  <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={advancedMode}
                      onChange={(e) => setAdvancedMode(e.target.checked)}
                      className="w-3.5 h-3.5 accent-[#52c41a]"
                    />
                    高级编辑模式
                  </label>
                </div>
                {!advancedMode && (
                  <div className="mt-3 grid grid-cols-2 gap-4 text-xs">
                    <div>
                      <span className="text-gray-500">输入：</span>
                      <span className="text-gray-700">{graphFields.inputs.map((i: any) => i.name).join('、') || '无'}</span>
                    </div>
                    <div>
                      <span className="text-gray-500">输出：</span>
                      <span className="text-gray-700">{graphFields.outputs.map((o: any) => o.name).join('、') || '无'}</span>
                    </div>
                  </div>
                )}
              </div>

              <div className="flex-1 min-h-0 neu-card rounded-xl overflow-hidden">
                {advancedMode ? (
                  <JdmConfigProvider>
                    <DndProvider backend={HTML5Backend}>
                      <DecisionGraph
                        value={graph}
                        onChange={(val) => setGraph(val as DecisionGraphType)}
                        simulate={simulate}
                        panels={panels}
                        defaultActivePanel="simulator"
                        mode="dev"
                      />
                    </DndProvider>
                  </JdmConfigProvider>
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-gray-400 text-sm text-center">
                    启用「高级编辑模式」后可拖拽节点、编辑决策表。
                    <br />
                    日常配置只需在「输入」「输出」两个标签页完成。
                  </div>
                )}
              </div>
            </div>
          )}

          {activeTab === 'output' && (
            <div className="flex flex-col gap-4 h-full">
              <div className="flex items-center justify-between">
                <label className="block text-xs font-medium text-gray-700">输出绑定</label>
                <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={showRawActions}
                    onChange={(e) => setShowRawActions(e.target.checked)}
                    className="w-3.5 h-3.5 accent-[#52c41a]"
                  />
                  显示原始控制动作
                </label>
              </div>

              {!showRawActions ? (
                <div className="neu-inset flex-1 overflow-y-auto p-3 text-xs">
                  {(config.outputBindings || []).length === 0 ? (
                    <div className="text-gray-400">当前决策表没有可绑定的输出字段</div>
                  ) : (
                    <table className="w-full">
                      <thead className="text-[10px] text-gray-500 border-b border-gray-200">
                        <tr>
                          <th className="text-left py-2 font-medium">决策输出</th>
                          <th className="text-left py-2 font-medium">设备实体实例</th>
                          <th className="text-left py-2 font-medium">操作</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-100">
                        {(config.outputBindings || []).map((binding, idx) => (
                          <tr key={binding.field}>
                            <td className="py-2">
                              <span className="font-medium text-gray-700">{binding.name}</span>
                              <span className="text-[10px] text-gray-400 ml-2">({binding.field})</span>
                            </td>
                            <td className="py-2">
                              <select
                                value={binding.entity_instance_id || ''}
                                onChange={(e) => {
                                  const entityInstanceId = e.target.value || undefined
                                  updateOutputBinding(idx, {
                                    entity_instance_id: entityInstanceId,
                                  })
                                }}
                                className="neu-input w-full px-2 py-1 text-xs bg-transparent"
                              >
                                <option value="">-- 选择实体实例 --</option>
                                {entityInstanceOptions.map((e) => (
                                  <option key={e.id} value={e.id}>{e.node_display_name} / {e.display_name}</option>
                                ))}
                              </select>
                            </td>
                            <td className="py-2">
                              <button
                                type="button"
                                onClick={() => testWrite({
                                  id: `output:${binding.field}`,
                                  type: 'control',
                                  entity_instance_id: binding.entity_instance_id,
                                  value: `{{${binding.field}}}`,
                                })}
                                disabled={!binding.entity_instance_id}
                                className="neu-btn px-2 py-1 text-[10px] text-[#389e0d] disabled:opacity-40"
                              >
                                测试下发
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              ) : (
                <div className="neu-inset flex-1 overflow-y-auto p-3 text-xs space-y-2">
                  {config.actions.map((action, idx) => (
                    <div key={idx} className="grid grid-cols-12 gap-2 items-center">
                      <select
                        value={action.entity_instance_id || ''}
                        onChange={(e) => {
                          const entityInstanceId = e.target.value || undefined
                          const actions = [...config.actions]
                          actions[idx] = {
                            ...action,
                            entity_instance_id: entityInstanceId,
                          }
                          setConfig({ ...config, actions })
                        }}
                        className="neu-input col-span-3 px-2 py-1 bg-transparent"
                      >
                        <option value="">-- 选择实体实例 --</option>
                        {entityInstanceOptions.map((e) => (
                          <option key={e.id} value={e.id}>{e.node_display_name} / {e.display_name}</option>
                        ))}
                      </select>
                      <input
                        value={action.id || ''}
                        onChange={(e) => {
                          const actions = [...config.actions]
                          actions[idx] = { ...action, id: e.target.value }
                          setConfig({ ...config, actions })
                        }}
                        placeholder="稳定动作 ID"
                        className="neu-input col-span-3 px-2 py-1"
                      />
                      <input
                        value={String(action.value ?? '')}
                        onChange={(e) => {
                          const actions = [...config.actions]
                          actions[idx] = { ...action, value: e.target.value }
                          setConfig({ ...config, actions })
                        }}
                        placeholder="值"
                        className="neu-input col-span-2 px-2 py-1"
                      />
                      <button
                        type="button"
                        onClick={() => testWrite(action)}
                        disabled={!action.entity_instance_id}
                        className="neu-btn col-span-1 px-1 py-1 text-[10px] text-[#389e0d] disabled:opacity-40"
                      >
                        测试
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    onClick={() =>
                      setConfig({
                        ...config,
                        actions: [...config.actions, {
                          id: crypto.randomUUID(),
                          type: 'control',
                          entity_instance_id: '',
                          value: '1',
                        }],
                      })
                    }
                    className="neu-btn px-2 py-1 text-[10px] text-gray-600"
                  >
                    + 添加控制命令
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </form>
    </div>
  )
}

function SimulateModal({
  rule,
  onClose,
}: {
  rule: Rule
  onClose: () => void
}) {
  const [context, setContext] = useState('{"pv_power": 120, "load_power": 80, "soc": 45, "tou_price": 0.35}')
  const [result, setResult] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleRun = async () => {
    setError('')
    setResult(null)
    let ctx: Record<string, any>
    try {
      ctx = JSON.parse(context)
    } catch {
      setError('上下文 JSON 格式错误')
      return
    }
    setLoading(true)
    try {
      const data = await simulateRule(rule.id, ctx)
      setResult(data)
    } catch (e: any) {
      setError(e.message || '模拟失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="neu-card w-[560px] max-w-[90vw] p-5">
        <h3 className="text-sm font-bold text-gray-800 mb-2">规则模拟: {rule.name}</h3>
        <p className="text-xs text-gray-500 mb-3">输入测试上下文 JSON，调用后端 zen-engine 评估。</p>
        <textarea
          value={context}
          onChange={(e) => setContext(e.target.value)}
          rows={6}
          className="neu-input w-full px-3 py-2 text-xs font-mono mb-3"
          placeholder='{"pv_power": 120, "load_power": 80, "soc": 45, "tou_price": 0.35}'
        />
        <div className="flex justify-between items-center">
          <button
            onClick={handleRun}
            disabled={loading}
            className="neu-btn px-4 py-1.5 text-xs font-medium text-[#389e0d] disabled:opacity-50"
          >
            {loading ? '运行中...' : '运行模拟'}
          </button>
          <button onClick={onClose} className="neu-btn px-4 py-1.5 text-xs text-gray-600">
            关闭
          </button>
        </div>
        {error && <div className="text-xs text-red-500 mt-3">{error}</div>}
        {result && (
          <div className="mt-3">
            <div className="text-xs text-gray-500 mb-1">模拟结果</div>
            <pre className="neu-inset p-3 text-[11px] font-mono text-gray-700 overflow-x-auto max-h-[240px]">
              {JSON.stringify(result, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </div>
  )
}

export default function RuleEnginePage() {
  const [rules, setRules] = useState<Rule[]>([])
  const [templates, setTemplates] = useState<RuleTemplate[]>([])
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState<Rule | null>(null)
  const [creating, setCreating] = useState(false)
  const [simulating, setSimulating] = useState<Rule | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const [rulesData, templatesData] = await Promise.all([fetchRules(), fetchRuleTemplates()])
      setRules(rulesData)
      setTemplates(templatesData)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const handleCreate = async (data: RuleCreateRequest) => {
    await createRule(data)
    load()
  }

  const handleUpdate = async (data: RuleCreateRequest) => {
    if (!editing) return
    await updateRule(editing.id, data)
    load()
  }

  const handleDelete = async (id: string) => {
    if (!confirm('确定删除该规则？')) return
    await deleteRule(id)
    load()
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-bold text-gray-800">规则引擎</h2>
          <p className="text-xs text-gray-500">管理 GoRules 决策图，为节点绑定规则。模板由后端配置驱动。</p>
        </div>
        <button
          onClick={() => setCreating(true)}
          className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d]"
        >
          + 新建规则
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {rules.map((rule) => (
          <div key={rule.id} className="neu-card p-4">
            <div className="flex items-start justify-between">
              <div>
                <h3 className="text-sm font-bold text-gray-800">{rule.name}</h3>
                <p className="text-[10px] text-gray-400 mt-0.5">v{rule.version} · {rule.id.slice(0, 8)}</p>
              </div>
              <span className={`px-2 py-0.5 rounded text-[10px] font-medium ${
                rule.enabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
              }`}>
                {rule.enabled ? '启用' : '禁用'}
              </span>
            </div>
            <div className="mt-3 text-xs text-gray-600">
              <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-[#534AB7]/10 text-[#534AB8] mr-2">
                {TYPE_LABELS[rule.rule_type]}
              </span>
              更新于 {new Date(rule.updated_at).toLocaleString('zh-CN', { hour12: false })}
            </div>
            <div className="mt-3 flex items-center gap-2">
              <button
                onClick={() => setSimulating(rule)}
                className="neu-btn px-3 py-1 text-xs text-[#389e0d]"
              >
                模拟
              </button>
              <button
                onClick={() => setEditing(rule)}
                className="neu-btn px-3 py-1 text-xs text-gray-600"
              >
                编辑
              </button>
              <button
                onClick={() => handleDelete(rule.id)}
                className="neu-btn px-3 py-1 text-xs text-red-500 hover:bg-red-50"
              >
                删除
              </button>
            </div>
          </div>
        ))}
        {rules.length === 0 && !loading && (
          <div className="neu-card p-8 text-center text-gray-400 text-sm col-span-full">
            暂无规则，点击右上角「新建规则」开始。
          </div>
        )}
      </div>

      {creating && (
        <RuleForm
          templates={templates}
          onSave={handleCreate}
          onCancel={() => setCreating(false)}
        />
      )}
      {editing && (
        <RuleForm
          initial={editing}
          templates={templates}
          onSave={handleUpdate}
          onCancel={() => setEditing(null)}
        />
      )}
      {simulating && (
        <SimulateModal
          rule={simulating}
          onClose={() => setSimulating(null)}
        />
      )}
    </div>
  )
}
