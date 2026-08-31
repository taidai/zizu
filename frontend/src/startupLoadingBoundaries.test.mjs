import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

function source(path) {
  return readFileSync(new URL(path, import.meta.url), 'utf8')
}

test('首屏和节点实时视图不提前加载重型编辑器与图表', () => {
  const main = source('./main.tsx')
  const nodeTree = source('./pages/NodeTreePage.tsx')
  const nodeTags = source('./components/NodeTagPanel.tsx')
  const viteConfig = source('../vite.config.ts')

  assert.doesNotMatch(main, /monaco|@gorules|ensureWasmLoaded/)
  assert.match(nodeTree, /lazy\(\(\) => import\('\.\.\/components\/data-trunk\/DataTrunkWorkspace'\)\)/)
  assert.match(nodeTags, /lazy\(\(\) => import\('\.\/RawPointHistoryPanel'\)\)/)
  assert.doesNotMatch(viteConfig, /return ['"]vendor['"]/)
})
