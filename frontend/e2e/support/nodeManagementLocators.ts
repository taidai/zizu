import type { Locator, Page } from '@playwright/test'

export function nodeTree(page: Page): Locator {
  return page.getByRole('region', { name: '真实节点树' })
}
