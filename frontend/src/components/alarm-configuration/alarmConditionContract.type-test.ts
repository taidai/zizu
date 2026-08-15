import type {
  AlarmCondition,
  AlarmConfigurationCurrent,
  LegacyAlarmMigrationCandidate,
} from '../../api/client'

const booleanCondition: AlarmCondition = { operator: 'eq', value: true }
const stringCondition: AlarmCondition = { operator: 'ne', value: '故障' }
const negativeNumberCondition: AlarmCondition = { operator: 'lt', value: -12.5 }

const currentConfiguration: AlarmConfigurationCurrent = {
  site_configuration_version: 3,
  definitions: [{
    entity_display_name: 'PCS 运行状态',
    rule_name: '停机告警',
    severity: 'MAJOR',
    trigger: booleanCondition,
    recovery: { operator: 'eq', value: false },
    source: 'legacy_migration',
    version_description: '旧配置迁移版',
    enabled: true,
    status: 'current',
  }],
}

const legacyCandidate: LegacyAlarmMigrationCandidate = {
  source_kind: 'tag_alarm',
  source_key: 'pcs-status',
  display_name: 'PCS 状态',
  status: 'ready',
  severity: 'MAJOR',
  entity_instance_id: 'entity-1',
  entity_instance_candidates: ['entity-1'],
  blockers: [],
  target_definition_ids: [],
  proposed_rules: [{
    entity_instance_id: 'entity-1',
    display_name: 'PCS 状态',
    blockers: [],
    proposed_definitions: [{
      name: '状态异常',
      severity: 'MAJOR',
      trigger: stringCondition,
      trigger_duration_seconds: 0,
      recovery: negativeNumberCondition,
      recovery_duration_seconds: 0,
      notification_throttle_seconds: 0,
      blockers: [],
    }],
  }],
}

void currentConfiguration
void legacyCandidate
