import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const SOURCES = [
  ['EntityDataPanel.tsx', /\[showPromotion, promotionBusy, promotionDirty\]/],
  ['InlinePointProcessingPanel.tsx', /\[expanded, busy, dirty\]/],
  ['PointProcessingTemplateManager.tsx', /\[draft !== null, draftDirty, busy, currentApplyBusy\]/],
]

test('dialog autofocus does not rerun when dirty or busy state changes', async () => {
  for (const [file, unsafeDependencies] of SOURCES) {
    const source = await readFile(new URL(`./${file}`, import.meta.url), 'utf8')
    assert.doesNotMatch(source, unsafeDependencies, `${file} must focus only when its dialog opens`)
  }
})
