import { expect, test, type BrowserContext, type Page } from '@playwright/test'

import { buildAcceptanceEnvironment } from './support/acceptanceEnvironment.mjs'
import { fixtureNames, runFixture } from './support/e2eFixture'

test.describe.serial('节点管理主干', () => {
  const environment = buildAcceptanceEnvironment(process.env)
  const names = fixtureNames(environment)
  const fixture = (command: Parameters<typeof runFixture>[0], value?: number) => (
    runFixture(command, value, environment)
  )
  const editedPlatformNode = `${names.platformNode}-已编辑`
  const entityDisplayName = `E2E有功功率-${environment.runId}`
  const entityDefinitionKey = `e2e.active_power_${environment.runId.replaceAll('-', '_')}`
  let context: BrowserContext
  let page: Page

  test.beforeAll(async ({ browser }) => {
    await fixture('setup')
    await fixture('preflight')
    context = await browser.newContext({ baseURL: environment.baseUrl })
    page = await context.newPage()
  })

  test.afterAll(async () => {
    try {
      await fixture('cleanup')
    } finally {
      await context?.close()
    }
  })

  test('登录并确认测试边界', async () => {
    await page.goto('/')
    const loginButton = page.getByRole('button', { name: '登录', exact: true })
    const nodeNavigation = page.getByRole('button', { name: '节点管理' })
    await Promise.race([
      loginButton.waitFor({ state: 'visible' }),
      nodeNavigation.waitFor({ state: 'visible' }),
    ])
    if (await loginButton.isVisible().catch(() => false)) {
      await page.getByLabel('用户名').fill(environment.username)
      await page.getByLabel('密码').fill(environment.password)
      await loginButton.click()
    }
    await expect(nodeNavigation).toBeVisible()
    await expect(page.getByText(environment.username, { exact: true })).toBeVisible()
  })

  test('节点可创建、编辑、搜索、刷新和选择', async () => {
    await page.getByRole('button', { name: '节点管理' }).click()
    await expect(page.getByRole('heading', { name: '节点管理' })).toBeVisible()
    const tree = nodeTree(page)
    const search = page.getByPlaceholder('搜索节点...')

    await Promise.all([
      page.waitForResponse((response) => (
        response.request().method() === 'GET'
        && response.url().includes('/api/v1/nodes')
        && response.ok()
      )),
      page.getByRole('button', { name: '刷新', exact: true }).click(),
    ])

    await search.fill(environment.writeRoot)
    const writeRoot = tree.getByTitle(environment.writeRoot, { exact: true }).first()
    const writeRootExists = await writeRoot.waitFor({ state: 'visible', timeout: 1_500 })
      .then(() => true, () => false)
    if (!writeRootExists) {
      await search.fill('')
      await page.getByRole('button', { name: '+ 节点', exact: true }).click()
      const modal = nodeModal(page, '新建节点')
      await modal.getByPlaceholder('例如：1# 储能电站').fill(environment.writeRoot)
      await modal.getByPlaceholder('例如：ESS / PV / Meter').fill('E2E_ROOT')
      await modal.locator('select').selectOption('')
      await modal.getByRole('button', { name: '保存', exact: true }).click()
      await expect(modal).toBeHidden()
      await search.fill(environment.writeRoot)
    }
    await expect(writeRoot).toBeVisible()
    await writeRoot.click()
    await expect(page.getByRole('heading', { name: environment.writeRoot, exact: true })).toBeVisible()

    await search.fill('')
    await page.getByRole('button', { name: '+ 节点', exact: true }).click()
    const createModal = nodeModal(page, '新建节点')
    await createModal.getByPlaceholder('例如：1# 储能电站').fill(names.platformNode)
    await createModal.getByPlaceholder('例如：ESS / PV / Meter').fill('PCS')
    await expect(createModal.locator('select')).toHaveValue(/.+/)
    await createModal.getByRole('button', { name: '保存', exact: true }).click()
    await expect(createModal).toBeHidden()

    await search.fill(names.platformNode)
    await expect(tree.getByTitle(names.platformNode)).toBeVisible()
    await tree.getByTitle(names.platformNode).click()
    await page.getByRole('button', { name: '编辑', exact: true }).click()
    const editModal = nodeModal(page, '编辑节点')
    await editModal.getByPlaceholder('例如：1# 储能电站').fill(editedPlatformNode)
    await editModal.getByRole('button', { name: '保存', exact: true }).click()
    await expect(editModal).toBeHidden()

    await search.fill(editedPlatformNode)
    await expect(tree.getByTitle(editedPlatformNode)).toBeVisible()
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    await expect(tree.getByTitle(editedPlatformNode)).toBeVisible()
    await tree.getByTitle(editedPlatformNode).click()
    await expect(page.getByRole('heading', { name: editedPlatformNode, exact: true })).toBeVisible()
  })

  test('Neuron 点位可预览导入且 L0 实时、历史、筛选和分页可用', async () => {
    await page.getByRole('button', { name: '导入点位', exact: true }).click()
    const modal = nodeModal(page, '从 Neuron 导入点位')
    const neuronSelect = modal.locator('select').first()
    await expect(neuronSelect.locator(`option[value="${names.neuronNode}"]`)).toHaveCount(1)
    await neuronSelect.selectOption(names.neuronNode)
    await expect(modal.getByRole('checkbox', { name: names.neuronGroup })).toBeChecked()
    await modal.getByRole('button', { name: '预览导入' }).click()
    await expect(modal.getByText('导入预览', { exact: true })).toBeVisible()

    const importDialog = page.waitForEvent('dialog')
    await modal.getByRole('button', { name: '确认导入' }).click()
    const dialog = await importDialog
    expect(dialog.message()).toMatch(/导入完成：新增 51 个/)
    await dialog.accept()
    await expect(modal).toBeHidden()

    await expect(page.getByText('共 51 个点位', { exact: true })).toBeVisible()
    await expect(page.getByText('第 1 / 2 页', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '下一页' }).click()
    await expect(page.getByText('e2e_spare_050', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '上一页' }).click()

    await page.getByLabel('数据类型').selectOption('FLOAT')
    await expect(page.getByText('共 51 个点位', { exact: true })).toBeVisible()
    await page.getByPlaceholder('搜索点位名称').fill(names.neuronTag)
    const realtimeRow = page.getByRole('row').filter({ hasText: names.neuronTag })
    await expect(realtimeRow).toBeVisible()

    await fixture('publish', 12.5)
    await expect(realtimeRow).toContainText('12.5', { timeout: 15_000 })
    await expect(realtimeRow).toContainText('正常')
    await expect(realtimeRow).toContainText(
      `${names.neuronNode}/${names.neuronGroup}/${names.neuronTag}`,
    )

    await page.getByRole('button', { name: '历史', exact: true }).click()
    await page.getByLabel('选择一个原始点位').selectOption({ label: names.neuronTag })
    await page.getByRole('button', { name: '明细', exact: true }).click()
    await expect(page.getByRole('cell', { name: '12.5', exact: true }).first()).toBeVisible()
  })

  test('L1 可检查发布且 L2 可查看实时、历史、质量和来源证据', async () => {
    await page.getByRole('button', { name: '原始数据', exact: true }).click()
    await page.getByRole('button', { name: '实时', exact: true }).click()
    await page.getByLabel('数据类型').selectOption('FLOAT')
    await page.getByPlaceholder('搜索点位名称').fill(names.neuronTag)
    await page.getByRole('checkbox', { name: `选择 ${names.neuronTag}` }).check()
    await page.getByRole('button', { name: '加工为实体', exact: true }).click()

    const editor = page.getByLabel('加工为实体')
    await editor.getByLabel('实体名称').fill(entityDisplayName)
    await editor.getByLabel('加工方法').selectOption('passthrough')
    await editor.getByLabel('单位').fill('kW')
    await editor.getByText('高级设置', { exact: true }).click()
    await editor.getByLabel('业务标识').fill(entityDefinitionKey)
    await editor.getByLabel('结果类型').selectOption('FLOAT')
    await editor.getByLabel('超时秒数').fill('30')

    await editor.getByRole('button', { name: '检查结果', exact: true }).click()
    await expect(editor.getByText('检查通过，可以发布。', { exact: true })).toBeVisible()
    await expect(editor.getByText('当前试算结果', { exact: true })).toBeVisible()
    await expect(editor).toContainText('12.5')
    await expect(editor).toContainText('1 个来源')

    await editor.getByRole('button', { name: '发布实体', exact: true }).click()
    await expect(editor.getByText(/标准实体已发布/)).toBeVisible()
    await fixture('publish', 13.5)

    await page.getByRole('button', { name: '标准实体', exact: true }).click()
    await expect(page.getByRole('heading', { name: '实体实时数据' })).toBeVisible()
    const entity = page.getByRole('button', { name: new RegExp(entityDisplayName) })
    await expect(entity).toBeVisible()
    await expect(entity).toContainText('13.5', { timeout: 15_000 })
    await expect(entity).toContainText('正常')
    await entity.click()
    await expect(page.getByRole('region', { name: '实体历史' })).toBeVisible()
    await expect(page.getByRole('region', { name: '实体来源' })).toContainText(names.neuronTag)
    await page.getByText('技术详情', { exact: true }).click()
    await expect(page.getByText(/processing_revision_id:/)).toBeVisible()
    await expect(page.getByText(/source_digest:/)).toBeVisible()
  })

  test('L1 模板可维护、升级、停用再恢复，规则可指定再取消且不会执行', async () => {
    test.setTimeout(180_000)
    await page.getByRole('button', { name: '标准实体', exact: true }).click()
    await page.getByRole('button', { name: '保存为共享模板', exact: true }).click()
    await page.getByPlaceholder('模板名称').fill(`E2E模板-${environment.runId}`)
    await page.getByPlaceholder('模板标识，如 pcs.site').fill(`e2e.template.${environment.runId.replaceAll('-', '_')}`)
    await page.getByPlaceholder('品牌').fill('E2E')
    await page.getByPlaceholder('型号').fill(environment.runId)
    await page.getByRole('button', { name: '确认保存', exact: true }).click()
    await expect(page.getByText('已保存为共享模板；当前节点运行配置没有改变。', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: '模板与版本', exact: true }).click()
    await expect(page.getByLabel('从哪个模板开始').locator('option:checked')).toContainText(
      `E2E模板-${environment.runId}`,
    )
    await page.getByRole('button', { name: '复制为下一修订', exact: true }).click()
    await page.getByRole('button', { name: '检查模板', exact: true }).click()
    await expect(page.getByText('检查通过，可以发布这个新版本。', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '发布新版本', exact: true }).click()
    await expect(page.getByRole('button', { name: '检查加工结果', exact: true })).toBeVisible()
    await page.getByRole('button', { name: '检查加工结果', exact: true }).click()
    await expect(page.getByText('检查通过', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '检查并发布', exact: true }).click()
    await expect(page.getByText('已生效', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: '模板与版本', exact: true }).click()
    await page.getByRole('button', { name: '编辑当前加工', exact: true }).click()
    await page.getByLabel('模板名称').fill(`${entityDisplayName}-加工`)
    await page.getByRole('button', { name: '检查修改', exact: true }).click()
    await expect(page.getByText('检查通过，可以发布当前加工的新修订。', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '发布修改', exact: true }).click()
    await expect(page.getByText('当前加工的新修订已发布。', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: '本节点配置', exact: true }).click()

    await page.getByRole('button', { name: '准备停用', exact: true }).click()
    await expect(page.getByText('停用预览', { exact: true })).toBeVisible()
    await expect(page.getByText(/历史值、来源证据和实体身份全部保留/)).toBeVisible()
    await page.getByRole('button', { name: '确认停用', exact: true }).click()
    await expect(page.getByText('已停用点位加工', { exact: true })).toBeVisible()
    await expect(page.getByText('当前节点还没有标准实体。请到“原始数据”勾选点位并定义数据来源与计算。', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: '检查加工结果', exact: true }).click()
    await page.getByRole('button', { name: '检查并发布', exact: true }).click()
    await expect(page.getByText('已生效', { exact: true })).toBeVisible()

    await fixture('ensure-rule')
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    await page.getByRole('button', { name: '指定规则', exact: true }).click()
    let modal = nodeModal(page, '为节点指定规则')
    const ruleName = `E2E规则-${environment.runId}`
    const e2eRule = modal.locator('label').filter({ hasText: ruleName }).getByRole('checkbox')
    await expect(e2eRule).toBeVisible()
    await e2eRule.check()
    await modal.getByRole('button', { name: '保存', exact: true }).click()
    await expect(page.getByText('已绑定规则:', { exact: true })).toBeVisible()
    await expect(page.getByText(ruleName, { exact: true })).toBeVisible()

    await page.getByRole('button', { name: '指定规则', exact: true }).click()
    modal = nodeModal(page, '为节点指定规则')
    await modal.locator('label').filter({ hasText: ruleName }).getByRole('checkbox').uncheck()
    await modal.getByRole('button', { name: '保存', exact: true }).click()
    await expect(page.getByText('已绑定规则:', { exact: true })).toBeHidden()
  })

  test('读取失败必须可见，临时节点最终退役', async () => {
    await page.route('**/api/v1/tags?**', async (route) => route.abort('failed'))
    await page.getByRole('button', { name: '原始数据', exact: true }).click()
    await expect(page.getByText('原始点位读取失败，请稍后重试', { exact: true })).toBeVisible()
    await page.unroute('**/api/v1/tags?**')

    await page.getByRole('button', { name: '标准实体', exact: true }).click()
    await page.getByRole('button', { name: '原始数据', exact: true }).click()
    await expect(page.getByText('共 51 个点位', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: '退役', exact: true }).click()
    const modal = nodeModal(page, '确认退役节点？')
    await expect(modal).toContainText(editedPlatformNode)
    await modal.getByRole('button', { name: '确认退役', exact: true }).click()
    await page.getByPlaceholder('搜索节点...').fill(editedPlatformNode)
    await expect(nodeTree(page).getByTitle(editedPlatformNode)).toHaveCount(0)
  })
})

function nodeTree(page: Page) {
  return page.locator('div.neu-card').filter({
    has: page.getByRole('heading', { name: '节点管理' }),
  }).first()
}

function nodeModal(page: Page, heading: string) {
  return page.locator('div.fixed.inset-0').filter({ hasText: heading }).first()
}
