import { useMemo, useState } from 'react'

import {
  exportPointProcessingTemplate,
  importPointProcessingTemplate,
  validatePointProcessingTemplate,
  type PointProcessingTemplate,
} from '../../api/client'
import {
  buildTransform,
  cloneTemplateDraft,
  formatEnumEntries,
  parseEnumEntries,
  visualTransformKind,
  type TemplateCopyMode,
  type TemplateDocument,
  type VisualTransformKind,
} from './pointProcessingTemplateEditorModel'

const DATA_TYPES = ['FLOAT', 'INT', 'BOOL', 'STRING', 'ENUM', 'CODE_SET']

export default function PointProcessingTemplateManager({
  templates,
  selectedRevisionId,
  onPublished,
}: {
  templates: PointProcessingTemplate[]
  selectedRevisionId: string
  onPublished: (revisionId: string) => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [baseRevisionId, setBaseRevisionId] = useState(selectedRevisionId)
  const [draft, setDraft] = useState<TemplateDocument | null>(null)
  const [draftMode, setDraftMode] = useState<TemplateCopyMode>('next-revision')
  const [enumTexts, setEnumTexts] = useState<Record<number, string>>({})
  const [checkedContent, setCheckedContent] = useState<Record<string, unknown> | null>(null)
  const [checkedDigest, setCheckedDigest] = useState('')
  const [busy, setBusy] = useState<'load' | 'check' | 'publish' | null>(null)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const activeBaseRevision = baseRevisionId || selectedRevisionId || templates[0]?.revision_id || ''

  const markDirty = () => {
    setCheckedContent(null)
    setCheckedDigest('')
    setSuccess('')
  }

  const patchDraft = (update: (current: TemplateDocument) => TemplateDocument) => {
    setDraft((current) => current ? update(current) : current)
    markDirty()
  }

  const loadDraft = async (mode: TemplateCopyMode) => {
    if (!activeBaseRevision) return
    setBusy('load')
    setError('')
    setSuccess('')
    try {
      const raw = await exportPointProcessingTemplate(activeBaseRevision) as TemplateDocument
      const next = cloneTemplateDraft(raw, mode)
      setDraftMode(mode)
      setDraft(next)
      setEnumTexts(Object.fromEntries(next.outputs.map((output, index) => [
        index,
        output.transform.kind === 'enum' ? formatEnumEntries(output.transform.entries) : '',
      ])))
      setCheckedContent(null)
      setCheckedDigest('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '读取模板失败')
    } finally {
      setBusy(null)
    }
  }

  const materializeDraft = (): TemplateDocument => {
    if (!draft) throw new Error('请先复制一个模板')
    return {
      ...draft,
      outputs: draft.outputs.map((output, index) => {
        if (output.transform.kind !== 'enum') return output
        return {
          ...output,
          transform: { ...output.transform, entries: parseEnumEntries(enumTexts[index]) },
        }
      }),
    }
  }

  const handleCheck = async () => {
    setBusy('check')
    setError('')
    setSuccess('')
    try {
      const content = materializeDraft()
      const result = await validatePointProcessingTemplate(content)
      setCheckedContent(result.content)
      setCheckedDigest(result.content_digest)
      setDraft(result.content as TemplateDocument)
      setSuccess('检查通过，可以发布这个新版本。')
    } catch (reason) {
      setCheckedContent(null)
      setCheckedDigest('')
      setError(reason instanceof Error ? reason.message : '检查模板失败')
    } finally {
      setBusy(null)
    }
  }

  const canPublish = useMemo(() => {
    if (!checkedContent || !draft) return false
    try {
      return JSON.stringify(materializeDraft()) === JSON.stringify(checkedContent)
    } catch {
      return false
    }
  }, [checkedContent, draft, enumTexts])

  const handlePublish = async () => {
    if (!checkedContent || !canPublish) return
    setBusy('publish')
    setError('')
    try {
      const result = await importPointProcessingTemplate(checkedContent)
      setSuccess(`新版本已发布（修订 ${draft?.revision}），现在可以在下方选择并安装。`)
      await onPublished(result.revision_id)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '发布模板失败')
    } finally {
      setBusy(null)
    }
  }

  const updateOutputTransform = (index: number, kind: VisualTransformKind) => {
    patchDraft((current) => ({
      ...current,
      outputs: current.outputs.map((output, outputIndex) => {
        if (outputIndex !== index) return output
        const previousInput = String(output.transform.input ?? current.inputs[0]?.id ?? '')
        return { ...output, transform: buildTransform(kind, previousInput, {
          scale: output.transform.scale ?? 1,
          offset: output.transform.offset ?? 0,
          minimum: output.transform.minimum ?? -1000000000,
          maximum: output.transform.maximum ?? 1000000000,
          entries: enumTexts[index] || '0=OFF\n1=ON',
          expression: output.transform.expression ?? previousInput,
        }) }
      }),
    }))
  }

  const patchInputContract = (index: number, field: string, value: unknown) => {
    patchDraft((current) => ({
      ...current,
      inputs: current.inputs.map((input, inputIndex) => inputIndex === index
        ? { ...input, sourceContract: { ...(input.sourceContract as Record<string, unknown>), [field]: value } }
        : input),
    }))
  }

  const patchTransformField = (index: number, field: string, value: unknown) => {
    patchDraft((current) => ({
      ...current,
      outputs: current.outputs.map((output, outputIndex) => outputIndex === index
        ? { ...output, transform: { ...output.transform, [field]: value } }
        : output),
    }))
  }

  return (
    <section className="rounded-xl border border-gray-200 bg-white/45">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        className="flex w-full items-center justify-between px-4 py-3 text-left"
      >
        <span>
          <span className="block text-sm font-semibold text-gray-900">维护共享模板</span>
          <span className="mt-0.5 block text-[11px] text-gray-500">管理员复制旧版本，修改加工方法，再发布新版本；不会改坏已运行的模板。</span>
        </span>
        <span className="text-xs font-medium text-blue-700">{open ? '收起' : '展开'}</span>
      </button>

      {open && (
        <div className="space-y-4 border-t border-gray-200 p-4">
          <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto_auto] lg:items-end">
            <label className="text-xs text-gray-600">
              从哪个模板开始
              <select
                value={activeBaseRevision}
                disabled={busy !== null}
                onChange={(event) => setBaseRevisionId(event.target.value)}
                className="neu-input mt-1 w-full bg-transparent px-3 py-2 text-xs"
              >
                {templates.map((template) => (
                  <option key={template.revision_id} value={template.revision_id}>
                    {template.display_name}｜{template.brand} {template.model}｜修订 {template.revision}
                  </option>
                ))}
              </select>
            </label>
            <button type="button" disabled={!activeBaseRevision || busy !== null} onClick={() => void loadDraft('next-revision')} className="neu-btn px-3 py-2 text-xs text-blue-700 disabled:opacity-50">
              复制为下一修订
            </button>
            <button type="button" disabled={!activeBaseRevision || busy !== null} onClick={() => void loadDraft('new-template')} className="neu-btn px-3 py-2 text-xs text-gray-700 disabled:opacity-50">
              另存为新模板
            </button>
          </div>

          {templates.length === 0 && <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">当前设备类型还没有模板，请先导入一份标准模板文件。</p>}
          {error && <p role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</p>}
          {success && <p className="rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-800">{success}</p>}

          {draft && (
            <fieldset disabled={busy !== null} className="space-y-4 disabled:opacity-70">
              <div className="rounded-lg border border-gray-200 bg-white/60 p-3">
                <h4 className="text-xs font-semibold text-gray-800">1. 模板是谁</h4>
                <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
                  {([
                    ['displayName', '模板名称'],
                    ['brand', '品牌'],
                    ['model', '型号'],
                    ['id', '模板标识'],
                  ] as const).map(([field, label]) => (
                    <label key={field} className="text-[11px] text-gray-500">
                      {label}
                      <input
                        value={String(draft[field])}
                        readOnly={draftMode === 'next-revision' && field !== 'displayName'}
                        onChange={(event) => patchDraft((current) => ({ ...current, [field]: event.target.value }))}
                        className="neu-input mt-1 w-full bg-transparent px-2 py-1.5 text-xs read-only:bg-gray-100 read-only:text-gray-500"
                      />
                    </label>
                  ))}
                  <label className="text-[11px] text-gray-500">
                    新修订号
                    <input type="number" min={1} value={draft.revision} readOnly className="neu-input mt-1 w-full bg-gray-100 px-2 py-1.5 text-xs text-gray-500" />
                  </label>
                </div>
              </div>

              <div className="rounded-lg border border-gray-200 bg-white/60 p-3">
                <h4 className="text-xs font-semibold text-gray-800">2. 设备数据叫什么</h4>
                <p className="mt-1 text-[11px] text-gray-500">这里写设备原始字段的标准名称和可能出现的别名，不写现场点位 UUID。</p>
                <div className="mt-3 max-h-72 space-y-2 overflow-y-auto pr-1">
                  {draft.inputs.map((input, index) => (
                    <div key={`${input.id}:${index}`} className="grid gap-2 rounded border border-gray-100 bg-gray-50/70 p-2 lg:grid-cols-[1fr_1.2fr_1.4fr_.8fr_.7fr_auto]">
                      <input aria-label="输入标识" value={String(input.id)} onChange={(event) => patchDraft((current) => ({ ...current, inputs: current.inputs.map((item, itemIndex) => itemIndex === index ? { ...item, id: event.target.value } : item) }))} className="neu-input bg-transparent px-2 py-1 text-xs" />
                      <input aria-label="来源名称" value={String(input.sourceKey ?? '')} onChange={(event) => patchDraft((current) => ({ ...current, inputs: current.inputs.map((item, itemIndex) => itemIndex === index ? { ...item, sourceKey: event.target.value } : item) }))} className="neu-input bg-transparent px-2 py-1 text-xs" />
                      <input aria-label="别名" value={Array.isArray(input.aliases) ? input.aliases.join(', ') : ''} onChange={(event) => patchDraft((current) => ({ ...current, inputs: current.inputs.map((item, itemIndex) => itemIndex === index ? { ...item, aliases: event.target.value.split(',').map((value) => value.trim()).filter(Boolean) } : item) }))} placeholder="别名，用逗号分开" className="neu-input bg-transparent px-2 py-1 text-xs" />
                      <select aria-label="数据类型" value={String(input.dataType ?? 'FLOAT')} onChange={(event) => patchDraft((current) => ({ ...current, inputs: current.inputs.map((item, itemIndex) => itemIndex === index ? { ...item, dataType: event.target.value } : item) }))} className="neu-input bg-transparent px-2 py-1 text-xs">
                        {DATA_TYPES.map((item) => <option key={item}>{item}</option>)}
                      </select>
                      <input aria-label="单位" value={String(input.unit ?? '')} onChange={(event) => patchDraft((current) => ({ ...current, inputs: current.inputs.map((item, itemIndex) => itemIndex === index ? { ...item, unit: event.target.value || null } : item) }))} placeholder="单位" className="neu-input bg-transparent px-2 py-1 text-xs" />
                      <label className="flex items-center gap-1 text-[11px] text-gray-600"><input type="checkbox" checked={Boolean(input.required)} onChange={(event) => patchDraft((current) => ({ ...current, inputs: current.inputs.map((item, itemIndex) => itemIndex === index ? { ...item, required: event.target.checked } : item) }))} />必需</label>
                      {Boolean(input.sourceContract) && typeof input.sourceContract === 'object' && (
                        <div className="grid gap-2 border-t border-gray-200 pt-2 lg:col-span-6 lg:grid-cols-4">
                          <input aria-label="协议组" value={String((input.sourceContract as Record<string, unknown>).group ?? '')} onChange={(event) => patchInputContract(index, 'group', event.target.value)} placeholder="协议组，例如 data" className="neu-input bg-transparent px-2 py-1 text-xs" />
                          <input aria-label="协议地址" value={String((input.sourceContract as Record<string, unknown>).address ?? '')} onChange={(event) => patchInputContract(index, 'address', event.target.value)} placeholder="寄存器地址" className="neu-input bg-transparent px-2 py-1 text-xs" />
                          <input aria-label="线类型" value={String((input.sourceContract as Record<string, unknown>).wireDataType ?? '')} onChange={(event) => patchInputContract(index, 'wireDataType', event.target.value)} placeholder="线类型，例如 INT16" className="neu-input bg-transparent px-2 py-1 text-xs" />
                          <input type="number" step="any" aria-label="协议倍率" value={(input.sourceContract as Record<string, unknown>).decimal == null ? '' : Number((input.sourceContract as Record<string, unknown>).decimal)} onChange={(event) => patchInputContract(index, 'decimal', event.target.value === '' ? null : Number(event.target.value))} placeholder="协议倍率，可空" className="neu-input bg-transparent px-2 py-1 text-xs" />
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>

              <div className="rounded-lg border border-gray-200 bg-white/60 p-3">
                <h4 className="text-xs font-semibold text-gray-800">3. 要加工成什么</h4>
                <p className="mt-1 text-[11px] text-gray-500">直通、倍率换算、枚举翻译和公式都在这里设置；已有高级故障解析可原样保留。</p>
                <div className="mt-3 space-y-3">
                  {draft.outputs.map((output, index) => {
                    const transformKind = visualTransformKind(output.transform)
                    return (
                      <div key={`${output.id}:${index}`} className="rounded border border-gray-100 bg-gray-50/70 p-3">
                        <div className="grid gap-2 lg:grid-cols-5">
                          <input aria-label="输出标识" value={String(output.id)} onChange={(event) => patchDraft((current) => ({ ...current, outputs: current.outputs.map((item, itemIndex) => itemIndex === index ? { ...item, id: event.target.value } : item) }))} className="neu-input bg-transparent px-2 py-1 text-xs" />
                          <input aria-label="实体定义" value={String(output.entityDefinition ?? '')} onChange={(event) => patchDraft((current) => ({ ...current, outputs: current.outputs.map((item, itemIndex) => itemIndex === index ? { ...item, entityDefinition: event.target.value } : item) }))} className="neu-input bg-transparent px-2 py-1 text-xs" />
                          <select aria-label="输出类型" value={String(output.dataType ?? 'FLOAT')} onChange={(event) => patchDraft((current) => ({ ...current, outputs: current.outputs.map((item, itemIndex) => itemIndex === index ? { ...item, dataType: event.target.value } : item) }))} className="neu-input bg-transparent px-2 py-1 text-xs">{DATA_TYPES.map((item) => <option key={item}>{item}</option>)}</select>
                          <input aria-label="输出单位" value={String(output.unit ?? '')} onChange={(event) => patchDraft((current) => ({ ...current, outputs: current.outputs.map((item, itemIndex) => itemIndex === index ? { ...item, unit: event.target.value || null } : item) }))} placeholder="单位" className="neu-input bg-transparent px-2 py-1 text-xs" />
                          <input aria-label="保鲜时间" value={String(output.freshness ?? '30s')} onChange={(event) => patchDraft((current) => ({ ...current, outputs: current.outputs.map((item, itemIndex) => itemIndex === index ? { ...item, freshness: event.target.value } : item) }))} placeholder="30s" className="neu-input bg-transparent px-2 py-1 text-xs" />
                        </div>
                        <div className="mt-2 grid gap-2 lg:grid-cols-[180px_180px_minmax(0,1fr)]">
                          <select value={transformKind} onChange={(event) => updateOutputTransform(index, event.target.value as VisualTransformKind)} className="neu-input bg-transparent px-2 py-1 text-xs">
                            {transformKind === 'preserve' && <option value="preserve">保持现有高级规则</option>}
                            <option value="passthrough">原值直通</option>
                            <option value="numeric">倍率 / 偏移</option>
                            <option value="enum">枚举翻译</option>
                            <option value="formula">公式计算</option>
                          </select>
                          {transformKind !== 'formula' && transformKind !== 'preserve' && (
                            <select value={String(output.transform.input ?? '')} onChange={(event) => patchDraft((current) => ({ ...current, outputs: current.outputs.map((item, itemIndex) => itemIndex === index ? { ...item, transform: { ...item.transform, input: event.target.value } } : item) }))} className="neu-input bg-transparent px-2 py-1 text-xs">
                              {draft.inputs.map((input) => <option key={input.id} value={input.id}>{input.id}</option>)}
                            </select>
                          )}
                          {transformKind === 'passthrough' && <p className="self-center text-[11px] text-gray-500">原值不变，直接形成实体。</p>}
                          {transformKind === 'numeric' && <div className="grid grid-cols-2 gap-2 xl:grid-cols-4"><input type="number" step="any" aria-label="倍率" title="倍率" value={Number(output.transform.scale ?? 1)} onChange={(event) => patchTransformField(index, 'scale', Number(event.target.value))} className="neu-input min-w-0 bg-transparent px-2 py-1 text-xs" /><input type="number" step="any" aria-label="偏移" title="偏移" value={Number(output.transform.offset ?? 0)} onChange={(event) => patchTransformField(index, 'offset', Number(event.target.value))} className="neu-input min-w-0 bg-transparent px-2 py-1 text-xs" /><input type="number" step="any" aria-label="最小值" title="最小值" value={Number(output.transform.minimum ?? -1000000000)} onChange={(event) => patchTransformField(index, 'minimum', Number(event.target.value))} className="neu-input min-w-0 bg-transparent px-2 py-1 text-xs" /><input type="number" step="any" aria-label="最大值" title="最大值" value={Number(output.transform.maximum ?? 1000000000)} onChange={(event) => patchTransformField(index, 'maximum', Number(event.target.value))} className="neu-input min-w-0 bg-transparent px-2 py-1 text-xs" /></div>}
                          {transformKind === 'enum' && <textarea aria-label="枚举映射" value={enumTexts[index] ?? ''} onChange={(event) => { setEnumTexts((current) => ({ ...current, [index]: event.target.value })); markDirty() }} placeholder={'0=STOPPED\n1=RUNNING'} rows={3} className="neu-input w-full resize-y bg-transparent px-2 py-1 text-xs" />}
                          {transformKind === 'formula' && <input aria-label="公式" value={String(output.transform.expression ?? '')} onChange={(event) => patchDraft((current) => ({ ...current, outputs: current.outputs.map((item, itemIndex) => itemIndex === index ? { ...item, transform: { ...item.transform, expression: event.target.value } } : item) }))} placeholder="sum(pcs_power)" className="neu-input w-full bg-transparent px-2 py-1 text-xs" />}
                          {transformKind === 'preserve' && <p className="self-center text-[11px] text-gray-500">这条规则会完整保留；如需简化，可改为左侧四种加工。</p>}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>

              <div className="flex flex-col gap-2 rounded-lg border border-blue-100 bg-blue-50/50 p-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="text-[11px] text-gray-600">
                  {checkedDigest ? `已检查：${checkedDigest.slice(0, 12)}…` : '先检查，检查通过后才能发布。发布只新增版本，不覆盖旧版本。'}
                </div>
                <div className="flex gap-2">
                  <button type="button" disabled={busy !== null} onClick={() => void handleCheck()} className="neu-btn px-4 py-2 text-xs font-medium text-blue-700 disabled:opacity-50">{busy === 'check' ? '检查中…' : '检查模板'}</button>
                  <button type="button" disabled={!canPublish || busy !== null} onClick={() => void handlePublish()} className="rounded-lg bg-[#52c41a] px-4 py-2 text-xs font-medium text-white disabled:cursor-not-allowed disabled:opacity-40">{busy === 'publish' ? '发布中…' : '发布新版本'}</button>
                </div>
              </div>
            </fieldset>
          )}
        </div>
      )}
    </section>
  )
}
