import assert from 'node:assert/strict'
import test from 'node:test'
import { pagesForArea, resolveTabletPage } from './appNavigationModel.ts'

test('operator navigation never opens configuration or strategy routes', () => {
  for (const page of ['tree', 'strategies', 'admin']) {
    assert.equal(resolveTabletPage('operator', page), 'workbench')
  }
  assert.deepEqual(pagesForArea('operator', 'engineering'), [])
})

test('engineering navigation preserves the admin-only tools boundary', () => {
  assert.equal(resolveTabletPage('engineer', 'admin'), 'workbench')
  assert.deepEqual(pagesForArea('engineer', 'engineering'), ['tree', 'alarms', 'strategies'])
  assert.deepEqual(pagesForArea('admin', 'engineering'), ['tree', 'alarms', 'strategies', 'admin'])
})

test('runtime navigation stays focused on overview monitoring and manual control', () => {
  for (const role of ['admin', 'engineer', 'operator']) {
    assert.deepEqual(pagesForArea(role, 'runtime'), ['workbench', 'monitor', 'controls'])
    for (const page of ['workbench', 'monitor', 'controls']) {
      assert.equal(resolveTabletPage(role, page), page)
    }
  }
})

test('alarm events remain reachable without becoming a runtime navigation destination', () => {
  assert.equal(resolveTabletPage('operator', 'alarms'), 'alarms')
  assert.equal(pagesForArea('operator', 'runtime').includes('alarms'), false)
})
