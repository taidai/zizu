import { useEffect, useMemo, useState, type ChangeEvent } from 'react'
import {
  applyInstallationPlan,
  createInstallationPlan,
  fetchSolutionInstallations,
  fetchSolutionPackages,
  importSolutionPackage,
  runDeliveryAcceptance,
  type DeliveryReport,
  type InstallationPlan,
  type SolutionInstallation,
  type SolutionPackage,
} from '../api/client'

interface Props {
  canImport: boolean
}

function parseParameter(type: string, raw: string): unknown {
  if (type === 'integer') return Number.parseInt(raw, 10)
  if (type === 'number' || type === 'port') return Number(raw)
  if (type === 'boolean') return raw === 'true'
  if (type === 'device_instances' || type === 'object' || type === 'array') return JSON.parse(raw)
  return raw
}

export default function SolutionDeliveryPage({ canImport }: Props) {
  const [packages, setPackages] = useState<SolutionPackage[]>([])
  const [installations, setInstallations] = useState<SolutionInstallation[]>([])
  const [selectedPackageId, setSelectedPackageId] = useState('')
  const [values, setValues] = useState<Record<string, string>>({})
  const [plan, setPlan] = useState<InstallationPlan | null>(null)
  const [report, setReport] = useState<DeliveryReport | null>(null)
  const [manualCommands, setManualCommands] = useState('{}')
  const [policyCommands, setPolicyCommands] = useState('{}')
  const [message, setMessage] = useState('')

  const selected = useMemo(
    () => packages.find((item) => item.id === selectedPackageId) || null,
    [packages, selectedPackageId],
  )

  const refresh = async () => {
    try {
      const [nextInstallations, nextPackages] = await Promise.all([
        fetchSolutionInstallations(),
        canImport ? fetchSolutionPackages() : Promise.resolve([]),
      ])
      setInstallations(nextInstallations)
      setPackages(nextPackages)
      if (!selectedPackageId && nextPackages[0]) setSelectedPackageId(nextPackages[0].id)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '无法读取交付状态。')
    }
  }

  useEffect(() => { void refresh() }, [canImport])

  const upload = async (event: ChangeEvent<HTMLInputElement>) => {
    const archive = event.target.files?.[0]
    if (!archive) return
    try {
      const imported = await importSolutionPackage(archive)
      setPackages((current) => [imported, ...current.filter((item) => item.id !== imported.id)])
      setSelectedPackageId(imported.id)
      setPlan(null)
      setMessage(`已导入并校验 ${imported.display_name}。请填写站点参数后创建安装计划。`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '解决方案包导入失败。')
    } finally {
      event.target.value = ''
    }
  }

  const createPlan = async () => {
    if (!selected) return
    try {
      const parameters: Record<string, unknown> = {}
      const secret_references: Record<string, string> = {}
      for (const contract of selected.parameter_contracts) {
        const raw = values[contract.id] ?? ''
        if (!raw) {
          if (contract.required) throw new Error(`请填写必填参数：${contract.id}`)
          continue
        }
        if (contract.type === 'secret') secret_references[contract.id] = raw
        else parameters[contract.id] = parseParameter(contract.type, raw)
      }
      const next = await createInstallationPlan(selected.id, { parameters, secret_references })
      setPlan(next)
      setMessage(next.blockers.length ? '安装计划包含阻断项，修正配置后重新创建计划。' : '安装计划已生成，请审查变更后执行。')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '安装计划创建失败。')
    }
  }

  const apply = async () => {
    if (!plan || plan.blockers.length) return
    try {
      const installation = await applyInstallationPlan(plan)
      setInstallations((current) => [installation, ...current.filter((item) => item.id !== installation.id)])
      setMessage(`已安装到站点配置版本 ${installation.site_configuration_version}。完成设备数据核验后可运行验收。`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '安装失败。')
    }
  }

  const runAcceptance = async (installation: SolutionInstallation) => {
    try {
      const manual = JSON.parse(manualCommands) as Record<string, string>
      const policy = JSON.parse(policyCommands) as Record<string, string>
      const next = await runDeliveryAcceptance(installation.id, {
        manual_commands: manual,
        policy_commands: policy,
      })
      setReport(next)
      setMessage(next.status === 'passed' ? '验收报告已通过。' : '验收报告已生成，其中存在未通过项。')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '验收运行失败。')
    }
  }

  return <section className="space-y-4">
    <div className="neu-card p-5"><h2 className="text-xl font-bold text-gray-800">解决方案交付</h2><p className="mt-1 text-xs text-gray-400">导入、审查、安装和机器验收均使用公开 API；敏感值只接受已受控保存的 Secret 引用。</p></div>
    {canImport ? <div className="neu-card p-5"><label className="block text-sm font-medium text-gray-700">导入已签收的解决方案包<input type="file" accept=".zip,.zizu.zip,application/zip" onChange={(event) => void upload(event)} className="mt-2 block w-full text-xs text-gray-600" /></label></div> : <div className="neu-card p-5 text-sm text-gray-500">当前角色可查看安装与验收结果；解决方案包导入由平台管理员完成。</div>}
    {selected && <div className="neu-card p-5"><div className="flex flex-wrap items-center justify-between gap-3"><label className="text-sm font-medium text-gray-700">解决方案<select value={selectedPackageId} onChange={(event) => { setSelectedPackageId(event.target.value); setPlan(null) }} className="neu-input ml-2 px-2 py-1.5 text-sm">{packages.map((item) => <option value={item.id} key={item.id}>{item.display_name} · {item.version}</option>)}</select></label><span className="font-mono-value text-[10px] text-gray-400">{selected.digest.slice(0, 16)}…</span></div><div className="mt-4 grid gap-3 md:grid-cols-2">{selected.parameter_contracts.map((contract) => <label key={contract.id} className="block text-xs font-medium text-gray-600">{contract.id}{contract.required && ' *'}<span className="ml-1 font-normal text-gray-400">{contract.description || contract.type}</span>{contract.type === 'boolean' ? <select value={values[contract.id] || ''} onChange={(event) => setValues({ ...values, [contract.id]: event.target.value })} className="neu-input mt-1 block w-full px-3 py-2 text-sm"><option value="">选择</option><option value="true">true</option><option value="false">false</option></select> : <textarea value={values[contract.id] || ''} onChange={(event) => setValues({ ...values, [contract.id]: event.target.value })} placeholder={contract.type === 'device_instances' ? '[{"instance_key":"PCS-01","device_key":"PCS-01"}]' : contract.type === 'secret' ? 'secret://approved-reference' : contract.type} className="neu-input mt-1 block min-h-10 w-full px-3 py-2 text-sm" />}</label>)}</div><button onClick={() => void createPlan()} className="mt-4 rounded-lg bg-[#52c41a] px-4 py-2 text-sm font-medium text-white">生成安装计划</button></div>}
    {plan && <div className="neu-card p-5"><h3 className="font-semibold text-gray-700">安装计划</h3><p className="mt-1 text-xs text-gray-400">{plan.status} · {plan.id}</p>{plan.blockers.length > 0 && <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-red-700">{plan.blockers.map((item) => <li key={item.code}>{item.code}: {item.message}</li>)}</ul>}<div className="mt-3 max-h-64 overflow-auto rounded-lg bg-gray-50 p-3 font-mono text-[11px] text-gray-600">{plan.items.map((item, index) => <pre key={index}>{JSON.stringify(item, null, 2)}</pre>)}</div><button disabled={plan.blockers.length > 0} onClick={() => void apply()} className="mt-4 rounded-lg bg-[#52c41a] px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50">确认安装</button></div>}
    <div className="neu-card p-5"><h3 className="font-semibold text-gray-700">已安装解决方案与验收</h3><label className="mt-3 block text-xs font-medium text-gray-600">手动控制命令映射（仅包要求手动控制回读验收时填写）<textarea value={manualCommands} onChange={(event) => setManualCommands(event.target.value)} className="neu-input mt-1 block min-h-10 w-full px-3 py-2 text-sm" /></label><label className="mt-3 block text-xs font-medium text-gray-600">策略命令映射（仅包要求策略执行验收时填写）<textarea value={policyCommands} onChange={(event) => setPolicyCommands(event.target.value)} className="neu-input mt-1 block min-h-10 w-full px-3 py-2 text-sm" /></label><div className="mt-3 space-y-2">{installations.map((installation) => <div key={installation.id} className="rounded-lg border border-white/60 bg-white/30 p-3 text-sm"><span>{installation.id} · 配置版本 {installation.site_configuration_version}</span><button onClick={() => void runAcceptance(installation)} className="ml-3 rounded-lg border border-gray-300 px-3 py-1 text-xs text-gray-700">运行验收</button></div>)}{installations.length === 0 && <p className="text-sm text-gray-400">尚无安装记录。</p>}</div>{report && <div className="mt-4 rounded-lg bg-gray-50 p-3"><p className="text-sm font-semibold text-gray-700">报告 {report.status}</p>{report.items.map((item) => <p className="mt-1 text-xs text-gray-600" key={item.acceptance_id}>{item.acceptance_id}: {item.status} · {item.code}</p>)}</div>}</div>
    {message && <p role="status" className="text-sm text-gray-600">{message}</p>}
  </section>
}
