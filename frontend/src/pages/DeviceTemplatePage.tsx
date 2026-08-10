import { useEffect, useState } from 'react'
import {
  fetchDeviceTemplates, createDeviceTemplate, updateDeviceTemplate, deleteDeviceTemplate,
  applyDeviceTemplate, fetchNodes, type DeviceTemplate, type Node,
} from '../api/client'

const DEFAULT_TEMPLATE_CONTENT = JSON.stringify({
  nodes: [
    {
      name: 'Device',
      node_type: 'DEVICE',
      tags: [
        {
          name: 'voltage',
          display_name: '电压',
          data_type: 'FLOAT',
          unit: 'V',
          read_write: 'R',
          source_path: '{prefix}/group/voltage',
          entity_name: 'device.voltage',
        },
        {
          name: 'current',
          display_name: '电流',
          data_type: 'FLOAT',
          unit: 'A',
          read_write: 'R',
          source_path: '{prefix}/group/current',
          entity_name: 'device.current',
        },
      ],
    },
  ],
}, null, 2)

export default function DeviceTemplatePage() {
  const [templates, setTemplates] = useState<DeviceTemplate[]>([])
  const [nodes, setNodes] = useState<Node[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<DeviceTemplate | null>(null)
  const [form, setForm] = useState({ name: '', category: '', description: '', content: DEFAULT_TEMPLATE_CONTENT })
  const [applyOpen, setApplyOpen] = useState(false)
  const [applyTemplate, setApplyTemplate] = useState<DeviceTemplate | null>(null)
  const [applyForm, setApplyForm] = useState({ parent_node_id: '', instance_name: '', source_prefix: '', brand: '' })
  const [applyResult, setApplyResult] = useState<any>(null)

  const load = async () => {
    setLoading(true)
    try {
      const [t, n] = await Promise.all([fetchDeviceTemplates(), fetchNodes()])
      setTemplates(t.items)
      setNodes(n)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const resetForm = (tpl?: DeviceTemplate | null) => {
    if (tpl) {
      setForm({
        name: tpl.name,
        category: tpl.category || '',
        description: tpl.description || '',
        content: JSON.stringify(tpl.content || {}, null, 2),
      })
    } else {
      setForm({ name: '', category: '', description: '', content: DEFAULT_TEMPLATE_CONTENT })
    }
  }

  const openCreate = () => {
    setEditing(null)
    resetForm()
    setModalOpen(true)
  }

  const openEdit = (tpl: DeviceTemplate) => {
    setEditing(tpl)
    resetForm(tpl)
    setModalOpen(true)
  }

  const openApply = (tpl: DeviceTemplate) => {
    setApplyTemplate(tpl)
    setApplyForm({ parent_node_id: nodes[0]?.id || '', instance_name: '', source_prefix: '', brand: '' })
    setApplyResult(null)
    setApplyOpen(true)
  }

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    let content
    try {
      content = JSON.parse(form.content)
    } catch {
      alert('模板内容不是有效 JSON')
      return
    }
    try {
      if (editing) {
        await updateDeviceTemplate(editing.id, {
          name: form.name,
          category: form.category || undefined,
          description: form.description || undefined,
          content,
        })
      } else {
        await createDeviceTemplate({
          name: form.name,
          category: form.category || undefined,
          description: form.description || undefined,
          content,
          enabled: true,
        })
      }
      setModalOpen(false)
      load()
    } catch (err: any) {
      alert(err.message || '保存失败')
    }
  }

  const handleDelete = async (tpl: DeviceTemplate) => {
    if (!confirm(`确定删除模板 \"${tpl.name}\"?`)) return
    try {
      await deleteDeviceTemplate(tpl.id)
      load()
    } catch (err: any) {
      alert(err.message || '删除失败')
    }
  }

  const handleApply = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!applyTemplate) return
    try {
      const res = await applyDeviceTemplate(applyTemplate.id, {
        parent_node_id: applyForm.parent_node_id,
        instance_name: applyForm.instance_name || undefined,
        source_prefix: applyForm.source_prefix || undefined,
        brand: applyForm.brand || undefined,
      })
      setApplyResult(res.summary)
      load()
    } catch (err: any) {
      alert(err.message || '应用失败')
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-bold text-gray-800">设备模板</h2>
          <p className="text-xs text-gray-500">把常用设备（PCS、BMS、电表等）的节点、点位、实体绑定预置为模板，一键下发到节点树。</p>
        </div>
        <button onClick={openCreate} className="neu-btn px-3 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d]">
          + 新建模板
        </button>
      </div>

      {loading && <div className="text-xs text-gray-400">加载中...</div>}

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {templates.map((tpl) => (
          <div key={tpl.id} className="neu-card p-3 flex flex-col">
            <div className="flex items-start justify-between">
              <div>
                <h3 className="text-sm font-bold text-gray-800">{tpl.name}</h3>
                {tpl.category && <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500">{tpl.category}</span>}
              </div>
              {tpl.is_system && <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-100 text-blue-600">系统</span>}
            </div>
            <p className="text-xs text-gray-500 mt-1 line-clamp-2">{tpl.description || '无描述'}</p>
            <div className="mt-auto pt-3 flex items-center gap-2">
              <button onClick={() => openApply(tpl)} className="neu-btn px-3 py-1 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d]">
                应用
              </button>
              <button onClick={() => openEdit(tpl)} className="neu-btn px-3 py-1 text-xs text-gray-600">
                编辑
              </button>
              {!tpl.is_system && (
                <button onClick={() => handleDelete(tpl)} className="neu-btn px-3 py-1 text-xs text-red-500 hover:text-red-700">
                  删除
                </button>
              )}
            </div>
          </div>
        ))}
        {templates.length === 0 && !loading && (
          <div className="neu-card p-8 text-center text-gray-400 text-sm">暂无模板</div>
        )}
      </div>

      {modalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
          <div className="neu-card w-[640px] max-w-[92vw] p-5 max-h-[90vh] overflow-y-auto">
            <h3 className="text-sm font-bold text-gray-800 mb-4">{editing ? '编辑模板' : '新建模板'}</h3>
            <form onSubmit={handleSave} className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-gray-600 mb-1">模板名 *</label>
                  <input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="neu-input w-full px-3 py-1.5 text-xs" />
                </div>
                <div>
                  <label className="block text-xs text-gray-600 mb-1">分类</label>
                  <input value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} className="neu-input w-full px-3 py-1.5 text-xs" placeholder="例如：PCS / BMS / 电表" />
                </div>
              </div>
              <div>
                <label className="block text-xs text-gray-600 mb-1">描述</label>
                <input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} className="neu-input w-full px-3 py-1.5 text-xs" />
              </div>
              <div>
                <label className="block text-xs text-gray-600 mb-1">模板内容 (JSON) *</label>
                <textarea
                  value={form.content}
                  onChange={(e) => setForm({ ...form, content: e.target.value })}
                  className="neu-input w-full px-3 py-2 text-xs font-mono"
                  rows={16}
                  spellCheck={false}
                />
                <p className="text-[10px] text-gray-400 mt-1">支持占位符：source_path 中使用 {prefix} 会被替换为应用时填写的来源前缀。</p>
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <button type="button" onClick={() => setModalOpen(false)} className="neu-btn px-4 py-1.5 text-xs text-gray-600">取消</button>
                <button type="submit" className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d]">保存</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {applyOpen && applyTemplate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
          <div className="neu-card w-[480px] max-w-[92vw] p-5">
            <h3 className="text-sm font-bold text-gray-800 mb-4">应用模板：{applyTemplate.name}</h3>
            <form onSubmit={handleApply} className="space-y-3">
              <div>
                <label className="block text-xs text-gray-600 mb-1">挂载到节点 *</label>
                <select
                  required
                  value={applyForm.parent_node_id}
                  onChange={(e) => setApplyForm({ ...applyForm, parent_node_id: e.target.value })}
                  className="neu-input w-full px-3 py-1.5 text-xs bg-transparent"
                >
                  <option value="">请选择父节点</option>
                  {nodes.map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.name} (L{n.layer})
                    </option>
                  ))}
                </select>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-gray-600 mb-1">实例名前缀</label>
                  <input value={applyForm.instance_name} onChange={(e) => setApplyForm({ ...applyForm, instance_name: e.target.value })} className="neu-input w-full px-3 py-1.5 text-xs" placeholder="例如：PCS_01" />
                </div>
                <div>
                  <label className="block text-xs text-gray-600 mb-1">来源前缀</label>
                  <input value={applyForm.source_prefix} onChange={(e) => setApplyForm({ ...applyForm, source_prefix: e.target.value })} className="neu-input w-full px-3 py-1.5 text-xs" placeholder="neuron/node/group" />
                </div>
              </div>
              <div>
                <label className="block text-xs text-gray-600 mb-1">品牌/型号</label>
                <input value={applyForm.brand} onChange={(e) => setApplyForm({ ...applyForm, brand: e.target.value })} className="neu-input w-full px-3 py-1.5 text-xs" placeholder="例如：Sungrow" />
              </div>
              {applyResult && (
                <div className="text-xs bg-gray-50 border border-gray-200 rounded p-2 space-y-1">
                  <div>节点：{applyResult.nodes_created} 个</div>
                  <div>点位：{applyResult.tags_created} 个</div>
                  <div>实体绑定：{applyResult.bindings_created} 个</div>
                  {applyResult.entity_missing.length > 0 && (
                    <div className="text-amber-600">未找到实体：{applyResult.entity_missing.join(', ')}</div>
                  )}
                  {applyResult.warnings.length > 0 && (
                    <div className="text-red-500">警告：{applyResult.warnings.join('; ')}</div>
                  )}
                </div>
              )}
              <div className="flex justify-end gap-2 pt-2">
                <button type="button" onClick={() => setApplyOpen(false)} className="neu-btn px-4 py-1.5 text-xs text-gray-600">关闭</button>
                <button type="submit" className="neu-btn px-4 py-1.5 text-xs font-medium text-white bg-[#52c41a] hover:bg-[#389e0d]">应用</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}
