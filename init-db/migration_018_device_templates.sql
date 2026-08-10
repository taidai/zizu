-- Migration 018: 设备模板表
-- 支持通过模板一键创建设备节点、点位，并绑定全局实体

CREATE TABLE IF NOT EXISTS t_device_templates (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    category    TEXT,
    description TEXT,
    content     JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_system   BOOLEAN DEFAULT FALSE,
    enabled     BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_device_templates_category ON t_device_templates(category);
CREATE INDEX IF NOT EXISTS idx_device_templates_enabled ON t_device_templates(enabled);

COMMENT ON TABLE t_device_templates IS '设备模板：定义一组节点、点位及实体绑定，用于快速接入同型号设备';
