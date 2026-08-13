-- ============================================================
-- ZiZu IoT Platform - 数据库 Schema 初始化脚本
-- 基于: g11-feature-domains.md v2.1 第9节
-- 数据库: TimescaleDB (PostgreSQL 16+)
-- 创建时间: 2026-07-17
-- ============================================================

-- 启用 TimescaleDB 扩展
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ══════════════════════════════════════
--  1. t_nodes: 统一节点表 (所有 5 层)
--  ══════════════════════════════════════
CREATE TABLE IF NOT EXISTS t_nodes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    parent_id   UUID REFERENCES t_nodes(id),
    layer       SMALLINT NOT NULL CHECK (layer BETWEEN 1 AND 5),
    node_type   TEXT NOT NULL,
    config      JSONB DEFAULT '{}',
    sort_order  INT DEFAULT 0,
    enabled     BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_nodes_parent ON t_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_layer ON t_nodes(layer);

COMMENT ON TABLE t_nodes IS 'ZiZu 统一节点表 - 所有5层(Site/Station/EnergyNode/Device/Tag)共用';

-- ══════════════════════════════════════
--  2. t_tags: 点位表 (Physical + Logical 统一)
--  ══════════════════════════════════════
CREATE TABLE IF NOT EXISTS t_tags (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    node_id       UUID NOT NULL REFERENCES t_nodes(id) ON DELETE CASCADE,
    tag_type      TEXT NOT NULL CHECK (tag_type IN ('PHYSICAL', 'LOGICAL')),
    data_type     TEXT NOT NULL CHECK (data_type IN ('FLOAT', 'INT', 'BOOL', 'STRING', 'ENUM')),
    name          TEXT NOT NULL,
    display_name  TEXT,
    unit          TEXT,
    read_write    TEXT DEFAULT 'R' CHECK (read_write IN ('R', 'RW', 'W')),

    -- PhysicalTag 字段
    source_type   TEXT DEFAULT 'NEURON',
    source_path   TEXT,
    scale_factor  FLOAT DEFAULT 1.0,
    value_offset  FLOAT DEFAULT 0.0,
    unit_from     TEXT,
    unit_to       TEXT,

    -- LogicalTag 字段
    formula       TEXT,
    formula_type  TEXT CHECK (formula_type IN ('expression', 'aggregate', 'condition')),
    sources       UUID[] DEFAULT '{}',
    aggregate_fn  TEXT CHECK (aggregate_fn IN ('SUM', 'AVG', 'MAX', 'MIN', 'COUNT', 'LAST')),

    range_min     FLOAT,
    range_max     FLOAT,
    enum_options  TEXT[],
    description   TEXT,
    sort_order    INT DEFAULT 0,
    enabled       BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tags_node ON t_tags(node_id);
CREATE INDEX IF NOT EXISTS idx_tags_type ON t_tags(tag_type);
CREATE INDEX IF NOT EXISTS idx_tags_sources ON t_tags USING GIN(sources);

COMMENT ON TABLE t_tags is 'ZiZu 点位表 - PhysicalTag(采集) + LogicalTag(公式/聚合) 统一存储';
COMMENT ON COLUMN t_tags.tag_type IS 'PHYSICAL=Neuron采集点位, LOGICAL=公式计算/聚合派生点位';

-- ══════════════════════════════════════
--  3. t_telemetry: 时序数据 Hypertable
--  ══════════════════════════════════════
CREATE TABLE IF NOT EXISTS t_telemetry (
    ts           TIMESTAMPTZ NOT NULL,
    node_id      UUID NOT NULL REFERENCES t_nodes(id) ON DELETE CASCADE,
    tag_id       UUID NOT NULL REFERENCES t_tags(id) ON DELETE CASCADE,
    value_float  FLOAT,
    value_int    BIGINT,
    value_bool   BOOLEAN,
    value_str    TEXT,
    is_virtual   BOOLEAN DEFAULT FALSE,
    quality      SMALLINT DEFAULT 192
);
-- 转换为 TimescaleDB Hypertable
SELECT create_hypertable('t_telemetry', 'ts', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_tel_node_tag ON t_telemetry(node_id, tag_id, ts DESC);
CREATE INDEX IF NOT EXISTS idxtel_virtual ON t_telemetry(is_virtual) WHERE is_virtual = TRUE;

COMMENT ON TABLE t_telemetry IS 'ZiZu 遥测主表 - 物理点位和虚拟点位共用, is_virtual 区分来源';
COMMENT ON COLUMN t_telemetry.quality IS 'OPC UA Quality: 192=GOOD, 64=UNCERTAIN, 0=BAD';

-- ═══ CAGG 连续聚合视图 (方案B Path B - 零Python代码) ═══

-- CAGG: 5 分钟连续聚合
CREATE MATERIALIZED VIEW IF NOT EXISTS tel_agg_5min
WITH (timescaledb.continuous) AS
SELECT time_bucket('5 minutes', ts) AS bucket,
       node_id, tag_id,
       avg(value_float) AS avg_val,
       min(value_float) AS min_val,
       max(value_float) AS max_val,
       count(*) AS count
FROM t_telemetry WHERE value_float IS NOT NULL
GROUP BY 1, 2, 3
WITH NO DATA;

-- CAGG: 1 小时连续聚合 (含物理值求和)
CREATE MATERIALIZED VIEW IF NOT EXISTS tel_agg_1h
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', ts) AS bucket,
       node_id, tag_id,
       avg(value_float) AS avg_val,
       sum(CASE WHEN is_virtual THEN 0 ELSE value_float END) AS sum_physical
FROM t_telemetry WHERE value_float IS NOT NULL
GROUP BY 1, 2, 3
WITH NO DATA;

-- CAGG: 1 天连续聚合
CREATE MATERIALIZED VIEW IF NOT EXISTS tel_agg_1d
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 day', ts) AS bucket,
       node_id, tag_id,
       avg(value_float) AS avg_val,
       max(value_float) AS max_daily,
       min(value_float) AS min_daily
FROM t_telemetry WHERE value_float IS NOT NULL
GROUP BY 1, 2, 3
WITH NO DATA;

-- 注: CAGG 连续聚合视图不支持 COMMENT ON MATERIALIZED VIEW (TimescaleDB 内部类型不同)
-- tel_agg_5min: CAGG 5分钟聚合 (方案B Path B)
-- tel_agg_1h:   CAGG 1小时聚合 (方案B Path B)
-- tel_agg_1d:   CAGG 1天聚合 (方案B Path B)


-- ═══════════════════════════════════════════════════════════════════
--  3.5 t_telemetry_latest: 每个 tag 最新值缓存表
-- ═══════════════════════════════════════════════════════════════════
-- 避免每次查历史 hypertable 做 DISTINCT ON；写入时由 pipeline 同步 upsert。
CREATE TABLE IF NOT EXISTS t_telemetry_latest (
    node_id     UUID NOT NULL REFERENCES t_nodes(id) ON DELETE CASCADE,
    tag_id      UUID NOT NULL REFERENCES t_tags(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL,
    value_float FLOAT,
    value_int   BIGINT,
    value_bool  BOOLEAN,
    value_str   TEXT,
    is_virtual  BOOLEAN DEFAULT FALSE,
    quality     SMALLINT DEFAULT 192,
    updated_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (node_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_telemetry_latest_tag ON t_telemetry_latest(tag_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_latest_node ON t_telemetry_latest(node_id);

COMMENT ON TABLE t_telemetry_latest IS 'ZiZu 遥测最新值缓存表 - 每个 tag 一行, 由 pipeline 同步维护';

-- ══════════════════════════════════════
--  4. t_rules: 规则表 (F2 / GoRules)
-- ══════════════════════════════════════
CREATE TABLE IF NOT EXISTS t_rules (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    rule_type   TEXT NOT NULL CHECK (rule_type IN ('alarm', 'control', 'fault_map', 'linkage')),
    jdm_content JSONB NOT NULL,
    version     INT DEFAULT 1,
    enabled     BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE t_rules IS 'ZiZu 规则表 - GoRules JDM 决策表/决策图存储';

-- ══════════════════════════════════════
--  5. t_audit_log: 审计日志 (F2 / RPC 操作)
-- ══════════════════════════════════════
CREATE TABLE IF NOT EXISTS t_audit_log (
    id          UUID DEFAULT gen_random_uuid(),
    user_id     TEXT,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   UUID,
    details     JSONB,
    ip_address  INET,
    created_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (id, created_at)
);
SELECT create_hypertable('t_audit_log', 'created_at', if_not_exists => TRUE);

COMMENT ON TABLE t_audit_log IS 'ZiZu 审计日志 - RPC操作/规则变更/登录记录';

-- ══════════════════════════════════════
--  6. t_alarms: 告警表 (F2 / GoRules 输出)
-- ══════════════════════════════════════
CREATE TABLE IF NOT EXISTS t_alarms (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id         UUID REFERENCES t_rules(id) ON DELETE CASCADE,
    node_id         UUID REFERENCES t_nodes(id) ON DELETE CASCADE,
    tag_id          UUID REFERENCES t_tags(id) ON DELETE CASCADE,
    trigger_tag_name TEXT,
    trigger_value   DOUBLE PRECISION,
    level           TEXT NOT NULL CHECK (level IN ('INFO', 'WARNING', 'MAJOR', 'CRITICAL')),
    message         TEXT NOT NULL,
    acknowledged    BOOLEAN DEFAULT FALSE,
    ack_user        TEXT,
    ack_at          TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_alarms_active ON t_alarms(level, acknowledged) WHERE resolved_at IS NULL;

COMMENT ON TABLE t_alarms IS 'ZiZu 告警表 - GoRules 触发输出';

-- ══════════════════════════════════════
--  7. t_users: 用户权限 (M0 基础)
-- ══════════════════════════════════════
CREATE TABLE IF NOT EXISTS t_users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username    TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role        TEXT DEFAULT 'viewer' CHECK (role IN ('admin', 'operator', 'viewer')),
    created_at  TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE t_users IS 'ZiZu 用户表 - 基础 RBAC: admin/operator/viewer';

-- 不播种任何默认管理员或公开密码。migration_021 应用完成后，使用
-- scripts/bootstrap_admin.py 的交互流程创建首个管理员。
