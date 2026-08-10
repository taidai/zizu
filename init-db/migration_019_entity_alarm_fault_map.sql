-- Migration 019: 实体告警绑定支持独立故障码映射
-- 允许实体-告警等级绑定引用 fault_map，覆盖点位自身的 fault_map

ALTER TABLE t_entity_alarm_bindings
    ADD COLUMN IF NOT EXISTS fault_map_id UUID REFERENCES t_fault_maps(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_entity_alarm_bindings_fault_map ON t_entity_alarm_bindings(fault_map_id);

COMMENT ON COLUMN t_entity_alarm_bindings.fault_map_id IS '绑定独立的故障码映射表（优先于点位 fault_map）';
