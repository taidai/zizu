-- ============================================================
-- migration_016: 告警类型 + 阈值 + 来源 + 累计计数 + 标准告警编码
-- 目的：对齐储能国标 (GB/T 36276, GB/T 19963, GB/T 51048, GB/T 22239)
--   1. t_tags 增加 alarm_type (过压/欠压/过流/过温/绝缘/通信中断/SOC超限/...)
--   2. t_tags 增加 alarm_threshold (阈值，用于测量型点位触发判定)
--   3. t_alarms 增加 alarm_type, alarm_threshold, alarm_source, alarm_count, alarm_code
-- ============================================================

-- 1) t_tags 扩展：告警类型 + 阈值
ALTER TABLE t_tags
    ADD COLUMN IF NOT EXISTS alarm_type TEXT,
    ADD COLUMN IF NOT EXISTS alarm_threshold DOUBLE PRECISION;

COMMENT ON COLUMN t_tags.alarm_type IS '告警类型: 过压/欠压/过流/过温/绝缘降低/通信中断/SOC超限/防孤岛/保护动作/消防告警/电弧故障/急停';
COMMENT ON COLUMN t_tags.alarm_threshold IS '告警阈值: 当点位值超过此阈值时触发告警 (测量型点位)';

-- 2) t_alarms 扩展：类型/阈值/来源/累计计数/标准编码
ALTER TABLE t_alarms
    ADD COLUMN IF NOT EXISTS alarm_type TEXT,
    ADD COLUMN IF NOT EXISTS alarm_threshold DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS alarm_source TEXT,
    ADD COLUMN IF NOT EXISTS alarm_count INT DEFAULT 1,
    ADD COLUMN IF NOT EXISTS alarm_code TEXT;

COMMENT ON COLUMN t_alarms.alarm_type IS '告警类型 (GB/T 36276 / GB/T 19963 分类)';
COMMENT ON COLUMN t_alarms.alarm_threshold IS '告警阈值';
COMMENT ON COLUMN t_alarms.alarm_source IS '告警来源: PV/ESS/PCS/EVSE/BMS/Grid/Environment/System';
COMMENT ON COLUMN t_alarms.alarm_count IS '同类告警累计次数';
COMMENT ON COLUMN t_alarms.alarm_code IS '标准告警编码 (IEC 61850 GOOSE / 自定义)';

-- 3) 索引
CREATE INDEX IF NOT EXISTS idx_alarms_type ON t_alarms(alarm_type) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_alarms_source_type ON t_alarms(alarm_source, alarm_type) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_tags_alarm_type ON t_tags(alarm_type);
