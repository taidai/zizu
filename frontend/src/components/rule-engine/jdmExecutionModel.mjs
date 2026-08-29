const REASON_LABELS = {
  JDM_INPUT_MISSING: '缺少输入数据',
  JDM_INPUT_QUALITY_NOT_GOOD: '输入质量不可用',
  JDM_INPUT_TIMESTAMP_MISSING: '输入时间缺失',
  JDM_MODEL_CONFIGURATION_MISMATCH: '规则与数据配置版本不一致',
  JDM_EVALUATION_FAILED: '规则计算失败',
}

export function isEditableJdmRuleType(ruleType) {
  return ruleType === 'control' || ruleType === 'linkage'
}

export function jdmExecutionLabel(execution) {
  if (!execution) return '尚无执行记录'
  if (execution.status === 'executed') return '已完成判断'
  const reason = REASON_LABELS[execution.reason_code] || execution.reason_code || '未知原因'
  return `已拒绝：${reason}`
}

export function latestJdmExecution(executions) {
  if (!Array.isArray(executions) || executions.length === 0) return null
  return [...executions].sort((left, right) => right.frame_sequence - left.frame_sequence)[0]
}
