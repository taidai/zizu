import { expect, type Page } from '@playwright/test'

/** Navigate through the same visible engineering entry used by the real user. */
export async function openEngineeringPage(page: Page, label: '节点与数据' | '告警' | '调度策略' | '系统工具') {
  const header = page.getByRole('banner')
  await expect(header.getByRole('button', { name: /^(工程配置|返回现场)$/ })).toBeVisible()
  const entry = header.getByRole('button', { name: '工程配置', exact: true })
  if (await entry.isVisible()) await entry.click()
  const target = page.getByRole('navigation', { name: '工程配置导航' })
    .getByRole('button', { name: label, exact: true })
  if (await target.getAttribute('aria-current') !== 'page') await target.click()
}
