import assert from 'node:assert/strict'
import test from 'node:test'

import {
  isEditableJdmRuleType,
  jdmExecutionLabel,
  latestJdmExecution,
} from './jdmExecutionModel.mjs'

test('keeps historical alarm models visible but read-only', () => {
  assert.equal(isEditableJdmRuleType('control'), true)
  assert.equal(isEditableJdmRuleType('linkage'), true)
  assert.equal(isEditableJdmRuleType('alarm'), false)
  assert.equal(isEditableJdmRuleType('fault_map'), false)
})

test('translates a rejected execution into an operator-readable reason', () => {
  assert.equal(
    jdmExecutionLabel({
      status: 'rejected',
      reason_code: 'JDM_INPUT_QUALITY_NOT_GOOD',
    }),
    '已拒绝：输入质量不可用',
  )
})

test('labels a successful judgment without implying device control success', () => {
  assert.equal(
    jdmExecutionLabel({status: 'executed', reason_code: null}),
    '已完成判断',
  )
})

test('selects the highest frame sequence as the latest fact', () => {
  assert.deepEqual(
    latestJdmExecution([
      {frame_sequence: 8, status: 'executed'},
      {frame_sequence: 10, status: 'rejected'},
      {frame_sequence: 9, status: 'executed'},
    ]),
    {frame_sequence: 10, status: 'rejected'},
  )
})
