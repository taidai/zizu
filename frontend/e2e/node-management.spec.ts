import { expect, test, type BrowserContext, type Page } from '@playwright/test'

import { buildAcceptanceEnvironment } from './support/acceptanceEnvironment.mjs'
import { fixtureNames, runFixture } from './support/e2eFixture'

test.describe.serial('节点管理主干', () => {
  const environment = buildAcceptanceEnvironment(process.env)
  const names = fixtureNames()
  const editedPlatformNode = `${names.platformNode}-已编辑`
  let context: BrowserContext
  let page: Page

  test.beforeAll(async ({ browser }) => {
    await runFixture('preflight')
    await runFixture('setup')
    context = await browser.newContext({ baseURL: environment.baseUrl })
    page = await context.newPage()
  })

  test.afterAll(async () => {
    try {
      await runFixture('cleanup')
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

    await search.fill(environment.writeRoot)
    const writeRoot = tree.getByTitle(environment.writeRoot, { exact: true }).first()
    if (await writeRoot.count() === 0) {
      await search.fill('')
      await page.getByRole('button', { name: '+ 节点', exact: true }).click()
      const modal = nodeModal(page, '新建节点')
      await modal.getByPlaceholder('例如：1# 储能电站').fill(environment.writeRoot)
      await modal.getByPlaceholder('例如：ESS / PV / Meter').fill('E2E_ROOT')
      await modal.locator('select').selectOption('')
      await modal.getByRole('button', { name: '保存', exact: true }).click()
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

    await search.fill(names.platformNode)
    await expect(tree.getByTitle(names.platformNode)).toBeVisible()
    await tree.getByTitle(names.platformNode).click()
    await page.getByRole('button', { name: '编辑', exact: true }).click()
    const editModal = nodeModal(page, '编辑节点')
    await editModal.getByPlaceholder('例如：1# 储能电站').fill(editedPlatformNode)
    await editModal.getByRole('button', { name: '保存', exact: true }).click()

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

    await runFixture('publish', 12.5)
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
})

function nodeTree(page: Page) {
  return page.locator('div.neu-card').filter({
    has: page.getByRole('heading', { name: '节点管理' }),
  }).first()
}

function nodeModal(page: Page, heading: string) {
  return page.locator('div.fixed.inset-0').filter({ hasText: heading }).first()
}
