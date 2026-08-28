import { useEffect, useMemo, useState } from 'react'
import {
  fetchNodes, fetchRules, updateNode, createNode, deleteNode, fetchAlarmCounts,
  fetchCategories, fetchNeuronNodes, fetchNeuronGroups, importNeuronTags,
  type Node, type Rule, type Category, type NeuronNode, type NeuronGroup,
} from '../api/client'
import NodeTagPanel from '../components/NodeTagPanel'
import DataTrunkWorkspace from '../components/data-trunk/DataTrunkWorkspace'
import { nodeDataTabs, type NodeDataTabKey } from '../components/data-trunk/dataTrunkViewModel'

type FormMode = 'create' | 'edit'

const LAYER_NAMES: Record<number, string> = {
  1: '站点 Site',
  2: '场站 Station',
  3: '能源节点 EnergyNode',
  4: '设备 Device',
  5: '逻辑节点 Tag',
}

function NodeIcon({ layer }: { layer: number }) {
  const colors: Record<number, string> = {
    1: '#3B6D11',
    2: '#185FA5',
    3: '#97C459',
    4: '#BA7517',
    5: '#85B7EB',
  }
  const shape = layer >= 4 ? 'rounded-full' : 'rounded'
  return (
    <span
      className={`inline-block w-2.5 h-2.5 ${shape} mr-2`}
      style={{ backgroundColor: colors[layer] || '#888' }}
    />
  )
}

interface TreeNodeProps {
  node: Node
  nodes: Node[]
  alarmCounts: Record<string, number>
  depth: number
  selectedId: string
  expanded: Set<string>
  onToggle: (id: string) => void
  onSelect: (id: string) => void
}

function TreeNode({ node, nodes, alarmCounts, depth, selectedId, expanded, onToggle, onSelect }: TreeNodeProps) {
  const children = useMemo(
    () => nodes.filter((n) => n.parent_id === node.id).sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name)),
    [nodes, node.id]
  )
  const isExpanded = expanded.has(node.id)
  const isSelected = selectedId === node.id
  const hasChildren = children.length > 0
  const alarmCount = alarmCounts[node.id] || 0

  return (
    <div>
      <div
        className={`flex items-center py-1.5 pr-2 cursor-pointer rounded-md transition-colors ${
          isSelected ? 'bg-[#52c41a]/15 text-gray-900' : 'hover:bg-white/40 text-gray-700'
        }`}
        style={{ paddingLeft: `${12 + depth * 16}px` }}
        onClick={() => onSelect(node.id)}
      >
        <span
          className={`w-4 h-4 mr-1 flex items-center justify-center text-[10px] text-gray-400 ${hasChildren ? 'hover:text-gray-600' : 'invisible'}`}
          onClick={(e) => { e.stopPropagation(); if (hasChildren) onToggle(node.id) }}
        >
          {hasChildren ? (isExpanded ? '▼' : '▶') : ''}
        </span>
        <NodeIcon layer={node.layer} />
        <span className="text-xs truncate" title={node.name}>{node.name}</span>
        {alarmCount > 0 && (
          <span className="ml-1.5 px-1.5 py-0.5 rounded-full text-[10px] font-medium bg-red-500 text-white">
            {alarmCount}
          </span>
        )}
        <span className="ml-auto text-[10px] text-gray-400">{node.tag_count > 0 ? node.tag_count : ''}</span>
      </div>
      {isExpanded && hasChildren && (
        <div>
          {children.map((child) => (
            <TreeNode
              key={child.id}
              node={child}
              nodes={nodes}
              alarmCounts={alarmCounts}
              depth={depth + 1}
              selectedId={selectedId}
              expanded={expanded}
              onToggle={onToggle}
              onSelect={onSelect}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function AssignRuleModal({
  node,
  rules,
  onClose,
  onSaved,
}: {
  node: Node
  rules: Rule[]
  onClose: () => void
  onSaved: () => void
}) {
  const currentIds = useMemo(() => {
    const ids = node.config?.rule_ids
    return Array.isArray(ids) ? new Set(ids as string[]) : new Set<string>()
  }, [node])
  const [selected, setSelected] = useState<Set<string>>(currentIds)
  const [saving, setSaving] = useState(false)

  const toggle = (id: string) => {
    const next = new Set(selected)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelected(next)
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      await updateNode(node.id, {
        config: { ...node.config, rule_ids: Array.from(selected) },
      })
      onSaved()
      onClose()
    } catch (e: any) {
      alert('保存失败：' + (e.message || e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="neu-card w-[480px] max-w-[90vw] p-5">
        <h3 className="text-sm font-bold text-gray-800 mb-3">为节点指定规则</h3>
        <p className="text-xs text-gray-500 mb-3">节点: <span className="font-medium text-gray-700">{node.name}</span></p>

        <div className="neu-inset p-3 max-h-[300px] overflow-y-auto space-y-2 mb-4">
          {rules.length === 0 && (
            <p className="text-xs text-gray-400">暂无规则，请先在「规则引擎」中创建。</p>
          )}
          {rules.map((rule) => (
            <label
              key={rule.id}
              className="flex items-center gap-2 p-2 rounded hover:bg-white/40 cursor-pointer"
            >
              <input
                type="checkbox"
                checked={selected.has(rule.id)}
                onChange={() => toggle(rule.id)}
                className="w-4 h-4 accent-[#52c41a]"
              />
              <div className="flex-1">
                <div className="text-xs font-medium text-gray-800">{rule.name}</div>
                <div className="text-[10px] text-gray-400">{rule.rule_type} · v{rule.version}</div>
              </div>
            </label>
          ))}
        </div>

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="neu-btn px-4 py-1.5 text-xs text-gray-600"
          >
            取消
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50"
          >
            {saving ? '保存中...' : '保存'}
          </button>
        </div>
      </div>
    </div>
  )
}

function NodeFormModal({
  mode,
  node,
  parentNode,
  categories,
  onClose,
  onSaved,
}: {
  mode: FormMode
  node?: Node
  parentNode?: Node
  categories: Category[]
  onClose: () => void
  onSaved: () => void
}) {
  const isCreate = mode === 'create'
  const defaultLayer = isCreate ? (parentNode ? parentNode.layer + 1 : 1) : (node?.layer || 1)
  const [name, setName] = useState(node?.name || '')
  const [nodeType, setNodeType] = useState(node?.node_type || '')
  const [sortOrder, setSortOrder] = useState(node?.sort_order ?? 0)
  const [enabled, setEnabled] = useState(node?.enabled ?? true)
  const [saving, setSaving] = useState(false)

  const typeOptions = useMemo(() => {
    const set = new Set(categories.map((c) => c.node_type))
    if (nodeType && !set.has(nodeType)) set.add(nodeType)
    return Array.from(set)
  }, [categories, nodeType])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    setSaving(true)
    try {
      if (isCreate) {
        await createNode({
          name: name.trim(),
          parent_id: parentNode?.id || null,
          layer: defaultLayer,
          node_type: nodeType.trim() || null,
          sort_order: Number(sortOrder) || 0,
          enabled,
        })
     } else if (node) {
       await updateNode(node.id, {
         name: name.trim(),
          node_type: nodeType.trim() || undefined,
         sort_order: Number(sortOrder) || 0,
         enabled,
       })
      }
      onSaved()
      onClose()
    } catch (e: any) {
      alert('保存失败：' + (e.message || e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="neu-card w-[420px] max-w-[90vw] p-5">
        <h3 className="text-sm font-bold text-gray-800 mb-3">{isCreate ? '新建节点' : '编辑节点'}</h3>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div>
            <label className="block text-xs text-gray-600 mb-1">节点名称</label>
            <input
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="neu-input w-full px-3 py-1.5 text-xs"
              placeholder="例如：1# 储能电站"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-600 mb-1">节点类型</label>
            <input
              list="node-type-options"
              value={nodeType}
              onChange={(e) => setNodeType(e.target.value)}
              className="neu-input w-full px-3 py-1.5 text-xs"
              placeholder="例如：ESS / PV / Meter"
            />
            <datalist id="node-type-options">
              {typeOptions.map((t) => (
                <option key={t} value={t} />
              ))}
            </datalist>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-gray-600 mb-1">层级</label>
              <input
                disabled
                value={LAYER_NAMES[defaultLayer] || defaultLayer}
                className="neu-input w-full px-3 py-1.5 text-xs bg-gray-100 text-gray-500"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-600 mb-1">排序</label>
              <input
                type="number"
                value={sortOrder}
                onChange={(e) => setSortOrder(Number(e.target.value))}
                className="neu-input w-full px-3 py-1.5 text-xs"
              />
            </div>
          </div>
          <label className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              className="w-4 h-4 accent-[#52c41a]"
            />
            启用
          </label>
          <div className="flex justify-end gap-2 pt-2">
            <button type="button" onClick={onClose} className="neu-btn px-4 py-1.5 text-xs text-gray-600">
              取消
            </button>
            <button
              type="submit"
              disabled={saving}
              className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50"
            >
              {saving ? '保存中...' : '保存'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function ImportNeuronModal({
  node,
  onClose,
  onSaved,
}: {
  node: Node
  onClose: () => void
  onSaved: () => void
}) {
  const [neuronNodes, setNeuronNodes] = useState<NeuronNode[]>([])
  const [groups, setGroups] = useState<NeuronGroup[]>([])
  const [selectedNode, setSelectedNode] = useState('')
  const [selectedGroup, setSelectedGroup] = useState('')
  const [loading, setLoading] = useState(false)
  const [importing, setImporting] = useState(false)

  useEffect(() => {
    setLoading(true)
    fetchNeuronNodes()
      .then((ns) => {
        setNeuronNodes(ns)
        if (ns.length > 0) setSelectedNode(ns[0].name)
      })
      .catch(() => alert('获取 Neuron 节点失败'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    if (!selectedNode) {
      setGroups([])
      return
    }
    fetchNeuronGroups(selectedNode)
      .then((gs) => {
        setGroups(gs)
        setSelectedGroup(gs[0]?.name || '')
      })
      .catch(() => setGroups([]))
  }, [selectedNode])

  const handleImport = async () => {
    if (!selectedNode || !selectedGroup) return
    setImporting(true)
    try {
      const res = await importNeuronTags({
        node_id: node.id,
        neuron_node: selectedNode,
        neuron_group: selectedGroup,
      })
      alert(`导入完成：新增 ${res.imported} 个，跳过 ${res.skipped} 个`)
      onSaved()
      onClose()
    } catch (e: any) {
      alert('导入失败：' + (e.message || e))
    } finally {
      setImporting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
      <div className="neu-card w-[420px] max-w-[90vw] p-5">
        <h3 className="text-sm font-bold text-gray-800 mb-2">从 Neuron 导入点位</h3>
        <p className="text-xs text-gray-500 mb-3">目标节点: <span className="font-medium text-gray-700">{node.name}</span></p>
        <div className="space-y-3">
          <div>
            <label className="block text-xs text-gray-600 mb-1">Neuron 节点</label>
            <select
              value={selectedNode}
              onChange={(e) => setSelectedNode(e.target.value)}
              disabled={loading}
              className="neu-input w-full px-3 py-1.5 text-xs bg-transparent"
            >
              {neuronNodes.map((n) => (
                <option key={n.name} value={n.name}>{n.name} ({n.plugin})</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-600 mb-1">Neuron 分组</label>
            <select
              value={selectedGroup}
              onChange={(e) => setSelectedGroup(e.target.value)}
              className="neu-input w-full px-3 py-1.5 text-xs bg-transparent"
            >
              {groups.map((g) => (
                <option key={g.name} value={g.name}>{g.name}</option>
              ))}
            </select>
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <button onClick={onClose} className="neu-btn px-4 py-1.5 text-xs text-gray-600">取消</button>
            <button
              onClick={handleImport}
              disabled={!selectedNode || !selectedGroup || importing}
              className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d] disabled:opacity-50"
            >
              {importing ? '导入中...' : '导入'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

export default function NodeTreePage({
  readOnly = false,
  actorId,
  canManageTemplates = false,
}: {
  readOnly?: boolean
  actorId: string
  canManageTemplates?: boolean
}) {
  const [nodes, setNodes] = useState<Node[]>([])
  const [rules, setRules] = useState<Rule[]>([])
  const [categories, setCategories] = useState<Category[]>([])
  const [selectedId, setSelectedId] = useState<string>('')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [activeTab, setActiveTab] = useState<NodeDataTabKey>('raw-points')
  const [showAssignModal, setShowAssignModal] = useState(false)
  const [nodeFormMode, setNodeFormMode] = useState<FormMode | null>(null)
  const [showImportModal, setShowImportModal] = useState(false)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [loading, setLoading] = useState(false)
  const [treeSearch, setTreeSearch] = useState('')
  const [alarmCounts, setAlarmCounts] = useState<Record<string, number>>({})

  const loadNodes = async () => {
    setLoading(true)
    try {
      const data = await fetchNodes()
      setNodes(data)
      if (data.length > 0 && !selectedId) {
        const roots = data.filter((n) => !n.parent_id)
        const firstId = roots[0]?.id || data[0].id
        setSelectedId(firstId)
        setExpanded(new Set(data.map((n) => n.id)))
      }
    } finally {
      setLoading(false)
    }
  }

  const loadRules = async () => {
    try {
      const data = await fetchRules()
      setRules(data)
    } catch {
      setRules([])
    }
  }

  const loadCategories = async () => {
    try {
      const data = await fetchCategories()
      setCategories(data)
    } catch {
      setCategories([])
    }
  }

  useEffect(() => {
    loadNodes()
    if (!readOnly) {
      loadRules()
      loadCategories()
    }
  }, [readOnly])

  useEffect(() => {
    if (readOnly && activeTab === 'point-processing') setActiveTab('raw-points')
  }, [activeTab, readOnly])

  useEffect(() => {
    if (nodes.length === 0) return
    fetchAlarmCounts(nodes.map((n) => n.id))
      .then(setAlarmCounts)
      .catch(() => {})
  }, [nodes])

  const filteredNodes = useMemo(() => {
    if (!treeSearch.trim()) return nodes
    const term = treeSearch.trim().toLowerCase()
    const matched = new Set<string>()
    // 先找匹配节点
    nodes.forEach((n) => {
      if (n.name.toLowerCase().includes(term) || (n.node_type || '').toLowerCase().includes(term)) {
        matched.add(n.id)
      }
    })
    // 把父节点链加进来
    const addAncestors = (id: string) => {
      const n = nodes.find((x) => x.id === id)
      if (n?.parent_id) {
        matched.add(n.parent_id)
        addAncestors(n.parent_id)
      }
    }
    matched.forEach(addAncestors)
    return nodes.filter((n) => matched.has(n.id))
  }, [nodes, treeSearch])

  const selectedNode = useMemo(
    () => nodes.find((n) => n.id === selectedId),
    [nodes, selectedId]
  )

  const handleToggle = (id: string) => {
    const next = new Set(expanded)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setExpanded(next)
  }

  const assignedRules = useMemo(() => {
    const ids = selectedNode?.config?.rule_ids
    if (!Array.isArray(ids)) return []
    return rules.filter((r) => ids.includes(r.id))
  }, [selectedNode, rules])

  const roots = useMemo(() => nodes.filter((n) => !n.parent_id).sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name)), [nodes])
  const filteredRoots = useMemo(() => filteredNodes.filter((n) => !n.parent_id).sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name)), [filteredNodes])

  const canAddChild = selectedNode ? selectedNode.layer < 5 : true

  const handleDelete = async () => {
    if (!selectedNode) return
    try {
      await deleteNode(selectedNode.id)
      setShowDeleteConfirm(false)
      setSelectedId('')
      loadNodes()
    } catch (e: any) {
      alert('删除失败：' + (e.message || e))
    }
  }

  return (
    <div className="flex gap-4 h-[calc(100vh-140px)] min-h-[500px]">
        {/* 左侧节点管理 */}
       <div className="neu-card w-80 flex flex-col p-3 overflow-hidden">
         <div className="flex items-center justify-between mb-2">
            <h2 className="text-sm font-bold text-gray-800">{readOnly ? '运行监控' : '节点管理'}</h2>
          <div className="flex items-center gap-1">
            {!readOnly && (
              <button
                onClick={() => setNodeFormMode('create')}
                disabled={!canAddChild}
                title={canAddChild ? '新建子节点' : '已是最深层级'}
                className="neu-btn px-2 py-1 text-[10px] font-medium text-[#389e0d] disabled:opacity-40"
              >
                + 子节点
              </button>
            )}
            <button
              onClick={loadNodes}
              disabled={loading}
              className="neu-btn px-2 py-1 text-[10px] text-gray-500 disabled:opacity-50"
            >
              刷新
            </button>
          </div>
        </div>
        <div className="mb-2">
          <input
            type="text"
            value={treeSearch}
            onChange={(e) => setTreeSearch(e.target.value)}
            placeholder="搜索节点..."
            className="neu-input w-full px-3 py-1.5 text-xs bg-transparent"
          />
        </div>
        <div className="flex-1 overflow-y-auto table-container pr-1">
          {filteredRoots.map((root) => (
            <TreeNode
              key={root.id}
              node={root}
              nodes={filteredNodes}
              alarmCounts={alarmCounts}
              depth={0}
              selectedId={selectedId}
              expanded={expanded}
              onToggle={handleToggle}
              onSelect={setSelectedId}
            />
          ))}
          {filteredRoots.length === 0 && !loading && (
            <div className="text-xs text-gray-400 py-4 text-center">
              {treeSearch ? '无匹配节点' : readOnly ? '暂无运行节点' : '暂无节点，点击「+ 子节点」创建根节点'}
            </div>
          )}
        </div>
      </div>

      {/* 右侧详情 */}
      <div className="flex-1 flex flex-col min-w-0">
        {selectedNode ? (
          <>
            <div className="neu-card p-4 mb-3">
              <div className="flex items-start justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <NodeIcon layer={selectedNode.layer} />
                    <h2 className="text-base font-bold text-gray-800">{selectedNode.name}</h2>
                    <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-gray-100 text-gray-600">
                      {LAYER_NAMES[selectedNode.layer] || `Layer ${selectedNode.layer}`}
                    </span>
                    {!selectedNode.enabled && (
                      <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-red-100 text-red-600">已禁用</span>
                    )}
                  </div>
                  <p className="text-xs text-gray-500 mt-1">设备类型：{selectedNode.node_type || '未设置'}　原始点位：{selectedNode.tag_count}</p>
                </div>
                {!readOnly && <div className="flex items-center gap-2">
                  <button
                    onClick={() => setShowImportModal(true)}
                    className="neu-btn px-3 py-1.5 text-xs text-gray-600"
                  >
                    导入点位
                  </button>
                  <button
                    onClick={() => setNodeFormMode('edit')}
                    className="neu-btn px-3 py-1.5 text-xs text-gray-600"
                  >
                    编辑
                  </button>
                  <button
                    onClick={() => setShowDeleteConfirm(true)}
                    className="neu-btn px-3 py-1.5 text-xs text-red-500 hover:bg-red-50"
                  >
                    删除
                  </button>
                  <button
                    onClick={() => setShowAssignModal(true)}
                    className="neu-btn px-4 py-1.5 text-xs font-medium text-[#389e0d]"
                  >
                    指定规则
                  </button>
                </div>}
              </div>

              {assignedRules.length > 0 && (
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <span className="text-xs text-gray-500">已绑定规则:</span>
                  {assignedRules.map((rule) => (
                    <span
                      key={rule.id}
                      className="px-2 py-0.5 rounded text-[10px] font-medium bg-[#534AB7]/10 text-[#534AB8]"
                    >
                      {rule.name}
                    </span>
                  ))}
                </div>
              )}
            </div>

            <div className="flex items-center gap-2 mb-3">
              {nodeDataTabs(readOnly).map((tab) => (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => setActiveTab(tab.key)}
                  className={`px-4 py-1.5 text-xs font-medium rounded-full transition-colors ${
                    activeTab === tab.key ? 'bg-[#52c41a] text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                  }`}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            <div className="flex-1 min-h-0 overflow-y-auto">
              {activeTab === 'raw-points' && <NodeTagPanel key={`${selectedNode.id}:raw`} nodeId={selectedNode.id} />}
              {activeTab === 'point-processing' && !readOnly && (
                <DataTrunkWorkspace key={`${selectedNode.id}:processing`} node={selectedNode} readOnly={false} actorId={actorId} canManageTemplates={canManageTemplates} view="processing" />
              )}
              {activeTab === 'entities' && (
                <DataTrunkWorkspace key={`${selectedNode.id}:entities`} node={selectedNode} readOnly={readOnly} actorId={actorId} canManageTemplates={canManageTemplates} view="entities" />
              )}
            </div>
          </>
        ) : (
          <div className="neu-card flex-1 flex items-center justify-center text-gray-400 text-sm">
            请选择左侧节点
          </div>
        )}
      </div>

      {!readOnly && showAssignModal && selectedNode && (
        <AssignRuleModal
          node={selectedNode}
          rules={rules}
          onClose={() => setShowAssignModal(false)}
          onSaved={loadNodes}
        />
      )}

      {!readOnly && nodeFormMode && (
        <NodeFormModal
          mode={nodeFormMode}
          node={nodeFormMode === 'edit' ? selectedNode : undefined}
          parentNode={nodeFormMode === 'create' ? selectedNode : undefined}
          categories={categories}
          onClose={() => setNodeFormMode(null)}
          onSaved={loadNodes}
        />
      )}

      {!readOnly && showImportModal && selectedNode && (
        <ImportNeuronModal
          node={selectedNode}
          onClose={() => setShowImportModal(false)}
          onSaved={loadNodes}
        />
      )}

      {!readOnly && showDeleteConfirm && selectedNode && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
          <div className="neu-card w-[360px] max-w-[90vw] p-5">
            <h3 className="text-sm font-bold text-gray-800 mb-2">确认删除节点？</h3>
            <p className="text-xs text-gray-500 mb-4">
              节点 <span className="font-medium text-gray-700">{selectedNode.name}</span> 及其所有子节点、挂载点位、快照与历史数据将被级联删除，不可恢复。
            </p>
            <div className="flex justify-end gap-2">
              <button onClick={() => setShowDeleteConfirm(false)} className="neu-btn px-4 py-1.5 text-xs text-gray-600">取消</button>
              <button onClick={handleDelete} className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-red-500 hover:bg-red-600">删除</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}


