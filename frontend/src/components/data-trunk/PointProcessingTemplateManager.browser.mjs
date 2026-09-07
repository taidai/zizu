// Run explicitly with node --test; normal model checks do not require a browser.
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { after, before, test } from 'node:test'
import { chromium, expect } from '@playwright/test'
import { createServer } from 'vite'

const document = {
  schemaVersion: 'zizu.point-processing/v1alpha1', kind: 'point_processing_template',
  id: 'test.pcs', displayName: 'PCS processing', deviceCategory: 'PCS',
  brand: 'Test', model: 'PCS', revision: 1, status: 'active',
  inputs: [{ id: 'power', sourceKind: 'l0', sourceKey: 'Power', aliases: [], dataType: 'FLOAT', unit: null, required: true }],
  outputs: [{ id: 'power', entityDefinition: 'pcs.power', dataType: 'FLOAT', unit: null, freshness: '30s', transform: { kind: 'passthrough', input: 'power' } }],
}
const plan = {
  id: 'edit-plan', node_id: 'node', template_revision_id: 'revision',
  base_configuration_revision: 1, status: 'ready', blockers: [], digest: 'a'.repeat(64),
  items: [{ item_key: 'power', kind: 'output_binding', layer: 'L2', action: 'update', output_id: 'power', entity_definition_id: 'pcs.power' }],
}

// Run the real React component. Only the API and parent-owned apply/refresh
// boundaries are controlled; no backend, database, or device is contacted.
const entry = `
import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import Manager from '/src/components/data-trunk/PointProcessingTemplateManager.tsx'
function Harness() {
  const [plan, setPlan] = useState(null)
  const [busy, setBusy] = useState(false)
  const [unknown, setUnknown] = useState(false)
  window.processingTest = {
    applied: () => setPlan(current => ({ ...current, status: 'applied' })),
    refreshed: () => setBusy(false),
    unknown: () => setUnknown(true),
  }
  return React.createElement(Manager, {
    templates: [], selectedRevisionId: 'revision', l0Points: [],
    nodeName: 'PCS', deviceCategory: 'PCS', nodeId: 'node', currentRevisionId: 'revision',
    currentInputBindings: { power: 'raw-power' }, currentPlan: plan,
    currentApplyBusy: busy, currentResultUnknown: unknown, canConfigure: true, canManage: true,
    onCurrentPlan: setPlan, onApplyCurrentPlan: () => setBusy(true), onPublished: async () => {},
  })
}
createRoot(document.getElementById('root')).render(React.createElement(Harness))
`

let server
let browser
let baseURL

before(async () => {
  server = await createServer({
    configFile: false,
    root: fileURLToPath(new URL('../../../', import.meta.url)),
    logLevel: 'error',
    esbuild: { jsx: 'automatic' },
    optimizeDeps: { noDiscovery: true, include: ['react', 'react-dom/client', 'react/jsx-dev-runtime'] },
    server: { host: '127.0.0.1', port: 0 },
    plugins: [{
      name: 'processing-manager-test',
      resolveId: (id) => id === '/__processing_test.tsx' ? '\0processing-test.tsx' : undefined,
      load: (id) => id === '\0processing-test.tsx' ? entry : undefined,
      configureServer(vite) {
        vite.middlewares.use((request, response, next) => {
          if (request.url !== '/__processing_test') return next()
          response.setHeader('Content-Type', 'text/html')
          response.end('<div id="root"></div><script type="module" src="/__processing_test.tsx"></script>')
        })
      },
    }],
  })
  await server.listen()
  baseURL = `http://127.0.0.1:${server.httpServer.address().port}`
  browser = await chromium.launch({ headless: true, channel: 'chromium' })
})

after(async () => {
  await browser?.close()
  await server?.close()
})

async function openEditor(t) {
  const page = await browser.newPage()
  page.setDefaultTimeout(5000)
  const pageErrors = []
  page.on('pageerror', (error) => pageErrors.push(error.message))
  t.after(() => assert.deepEqual(pageErrors, []))
  t.after(() => page.close())
  const dialogs = []
  page.on('dialog', async (dialog) => {
    dialogs.push(dialog.message())
    await dialog.dismiss()
  })
  await page.route('**/api/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (!path.startsWith('/api/')) return route.continue()
    if (path.endsWith('/revision/export')) return route.fulfill({ json: document })
    if (path.endsWith('/point-processing-drafts/plan')) return route.fulfill({ json: plan })
    return route.fulfill({ status: 503, json: { detail: 'Unexpected isolated test request' } })
  })
  await page.goto(`${baseURL}/__processing_test`)
  await page.getByRole('button', { name: '编辑当前加工', exact: true }).click()
  const editor = page.getByRole('dialog', { name: '编辑当前加工' })
  await expect(editor).toBeVisible()
  await editor.getByLabel('输出单位', { exact: true }).fill('kW')
  return { page, editor, dialogs }
}

async function startApply(editor) {
  await editor.getByRole('button', { name: '检查修改', exact: true }).click()
  await editor.getByRole('button', { name: '发布修改', exact: true }).click()
  await expect(editor.getByLabel('输出单位', { exact: true })).toBeDisabled()
}

test('confirmed apply can close while runtime refresh is still busy', async (t) => {
  const { page, editor, dialogs } = await openEditor(t)
  await startApply(editor)
  await page.evaluate(() => window.processingTest.applied())
  await expect(editor).toContainText('当前加工的新修订已发布。')
  await expect(editor.getByLabel('输出单位', { exact: true })).toBeDisabled()
  await editor.getByRole('button', { name: '取消', exact: true }).click()
  await expect(editor).not.toBeVisible({ timeout: 1500 })
  assert.deepEqual(dialogs, [])
})

test('in-flight and unknown apply cannot dismiss the editor', async (t) => {
  const { page, editor, dialogs } = await openEditor(t)
  await startApply(editor)
  await expect(editor.getByRole('button', { name: '取消', exact: true })).toBeDisabled()
  await page.keyboard.press('Escape')
  await expect(editor).toBeVisible()
  await page.evaluate(() => window.processingTest.unknown())
  await page.keyboard.press('Escape')
  await expect(editor).toBeVisible()
  await page.evaluate(() => window.processingTest.applied())
  await expect(editor.getByRole('button', { name: '取消', exact: true })).toBeDisabled()
  await page.keyboard.press('Escape')
  await expect(editor).toBeVisible()
  assert.deepEqual(dialogs, [])
})

test('unpublished edits still ask before discarding and survive refusal', async (t) => {
  const { editor, dialogs } = await openEditor(t)
  await editor.getByRole('button', { name: '取消', exact: true }).click()
  await expect(editor).toBeVisible()
  await expect(editor.getByLabel('输出单位', { exact: true })).toHaveValue('kW')
  assert.deepEqual(dialogs, ['放弃尚未发布的加工修改？'])
})

test('editing again after a confirmed apply restores discard protection', async (t) => {
  const { page, editor, dialogs } = await openEditor(t)
  await startApply(editor)
  await page.evaluate(() => {
    window.processingTest.applied()
    window.processingTest.refreshed()
  })
  await expect(editor).toContainText('当前加工的新修订已发布。')
  await editor.getByLabel('模板名称', { exact: true }).fill('New unpublished name')
  await editor.getByRole('button', { name: '取消', exact: true }).click()
  await expect(editor).toBeVisible()
  assert.deepEqual(dialogs, ['放弃尚未发布的加工修改？'])
})
