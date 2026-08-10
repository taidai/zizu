-- ============================================================
-- migration_017: 自定义告警等级 + 全局实体批量绑定
-- 目的：
--   1. 支持用户自定义告警等级（替代/扩展 error1/error2/error3）
--   2. 支持为全局实体批量配置告警等级与触发规则
--   3. 保留 t_tags.alarm_level 兼容旧配置
-- ============================================================

-- 1) 告警等级表
CREATE TABLE IF NOT EXISTS t_alarm_levels (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('CRITICAL', 'MAJOR', 'WARNING', 'INFO')),
    color         TEXT,
    trigger_rules JSONB NOT NULL DEFAULT '[]',
    enabled       BOOLEAN DEFAULT TRUE,
    sort_order    INT DEFAULT 0,
    is_system     BOOLEAN DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE t_alarm_levels IS '自定义告警等级表';
COMMENT ON COLUMN t_alarm_levels.code IS '等级编码，用于告警 source_key（如 error1/error2/error3）';
COMMENT ON COLUMN t_alarm_levels.severity IS '映射到 t_alarms.level: CRITICAL/MAJOR/WARNING/INFO';
COMMENT ON COLUMN t_alarm_levels.trigger_rules IS '触发规则 JSON 数组，支持 active/eq/gte/lte/fault 等';

-- 2) 实体-告警等级绑定表
CREATE TABLE IF NOT EXISTS t_entity_alarm_bindings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       UUID NOT NULL REFERENCES t_entities(id) ON DELETE CASCADE,
    alarm_level_id  UUID NOT NULL REFERENCES t_alarm_levels(id) ON DELETE CASCADE,
    trigger_rules   JSONB, -- 可选：覆盖告警等级的默认规则
    enabled         BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(entity_id, alarm_level_id)
);

COMMENT ON TABLE t_entity_alarm_bindings IS '全局实体与告警等级的绑定关系';
COMMENT ON COLUMN t_entity_alarm_bindings.trigger_rules IS '覆盖规则，NULL 表示使用 alarm_level 默认规则';

CREATE INDEX IF NOT EXISTS idx_entity_alarm_bindings_entity ON t_entity_alarm_bindings(entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_alarm_bindings_level ON t_entity_alarm_bindings(alarm_level_id);

-- 3) 预置三级告警等级（兼容现有 error1/error2/error3）
INSERT INTO t_alarm_levels (code, name, severity, color, trigger_rules, sort_order, is_system, enabled)
VALUES
    ('error1', '严重告警', 'CRITICAL', '#ef4444', '[{"op":"active"}]', 0, TRUE, TRUE),
    ('error2', '重要告警', 'MAJOR',    '#f97316', '[{"op":"active"}]', 1, TRUE, TRUE),
    ('error3', '一般告警', 'WARNING',  '#eab308', '[{"op":"active"}]', 2, TRUE, TRUE)
ON CONFLICT (code) DO UPDATE SET
    name       = EXCLUDED.name,
    severity   = EXCLUDED.severity,
    color      = EXCLUDED.color,
    sort_order = EXCLUDED.sort_order,
    is_system  = TRUE,
    updated_at = now()
WHERE t_alarm_levels.is_system = TRUE;

-- 4) t_alarms 扩展：支持全局实体告警来源
ALTER TABLE t_alarms
    ADD COLUMN IF NOT EXISTS entity_id UUID REFERENCES t_entities(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_alarms_entity ON t_alarms(entity_id, source_key) WHERE resolved_at IS NULL;

COMMENT ON COLUMN t_alarms.entity_id IS '触发告警的全局实体 ID（F4 实体告警）';
