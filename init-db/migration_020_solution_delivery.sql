-- Migration 020: 版本化解决方案包、安装计划与机器验收报告

CREATE TABLE IF NOT EXISTS t_solution_packages (
    id UUID PRIMARY KEY,
    package_id TEXT NOT NULL,
    version TEXT NOT NULL,
    display_name TEXT NOT NULL,
    digest CHAR(64) NOT NULL,
    status TEXT NOT NULL CHECK (status = 'validated'),
    acceptance_ids JSONB NOT NULL,
    manifest JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (package_id, version),
    UNIQUE (digest)
);

CREATE TABLE IF NOT EXISTS t_solution_package_assets (
    package_record_id UUID NOT NULL REFERENCES t_solution_packages(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    content BYTEA NOT NULL,
    PRIMARY KEY (package_record_id, path)
);

CREATE TABLE IF NOT EXISTS t_solution_install_plans (
    id UUID PRIMARY KEY,
    package_record_id UUID NOT NULL REFERENCES t_solution_packages(id),
    package_digest CHAR(64) NOT NULL,
    base_site_configuration_version BIGINT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready', 'blocked')),
    items JSONB NOT NULL,
    blockers JSONB NOT NULL,
    digest CHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t_solution_installations (
    id UUID PRIMARY KEY,
    plan_id UUID NOT NULL REFERENCES t_solution_install_plans(id),
    package_record_id UUID NOT NULL REFERENCES t_solution_packages(id),
    package_digest CHAR(64) NOT NULL,
    site_configuration_version BIGINT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status = 'installed'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t_site_configuration_versions (
    version BIGINT PRIMARY KEY CHECK (version >= 0),
    previous_version BIGINT NULL REFERENCES t_site_configuration_versions(version),
    installation_id UUID NULL UNIQUE REFERENCES t_solution_installations(id),
    package_record_id UUID NULL REFERENCES t_solution_packages(id),
    package_digest CHAR(64) NULL,
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (version = 0 AND previous_version IS NULL AND installation_id IS NULL
         AND package_record_id IS NULL AND package_digest IS NULL)
        OR
        (version > 0 AND previous_version = version - 1 AND installation_id IS NOT NULL
         AND package_record_id IS NOT NULL AND package_digest IS NOT NULL)
    )
);
INSERT INTO t_site_configuration_versions
  (version, previous_version, installation_id, package_record_id,
   package_digest, actor)
VALUES (0, NULL, NULL, NULL, NULL, 'system-bootstrap')
ON CONFLICT (version) DO NOTHING;

CREATE TABLE IF NOT EXISTS t_site_configuration_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    current_version BIGINT NOT NULL DEFAULT 0
        REFERENCES t_site_configuration_versions(version)
);
INSERT INTO t_site_configuration_state (singleton, current_version)
VALUES (TRUE, 0)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS t_solution_delivery_audit (
    id UUID PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action = 'solution.install'),
    installation_id UUID NOT NULL REFERENCES t_solution_installations(id),
    package_record_id UUID NOT NULL REFERENCES t_solution_packages(id),
    package_digest CHAR(64) NOT NULL,
    site_configuration_version BIGINT NOT NULL
        REFERENCES t_site_configuration_versions(version),
    details JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (action, installation_id)
);

CREATE TABLE IF NOT EXISTS t_delivery_reports (
    id UUID PRIMARY KEY,
    installation_id UUID NOT NULL REFERENCES t_solution_installations(id),
    platform_version TEXT NOT NULL,
    package_id TEXT NOT NULL,
    package_version TEXT NOT NULL,
    package_digest CHAR(64) NOT NULL,
    site_configuration_version BIGINT NOT NULL,
    actor TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    duration_ms BIGINT NOT NULL CHECK (duration_ms >= 0),
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    items JSONB NOT NULL,
    digest CHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t_delivery_idempotency (
    command_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest CHAR(64) NOT NULL,
    installation_id UUID NULL REFERENCES t_solution_installations(id),
    report_id UUID NULL REFERENCES t_delivery_reports(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (command_type, actor, idempotency_key),
    CHECK (
        (command_type = 'apply_install' AND installation_id IS NOT NULL
         AND report_id IS NULL)
        OR
        (command_type = 'run_acceptance' AND report_id IS NOT NULL
         AND installation_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_solution_packages_identity
    ON t_solution_packages(package_id, version);
CREATE INDEX IF NOT EXISTS idx_solution_installations_package
    ON t_solution_installations(package_record_id);
CREATE INDEX IF NOT EXISTS idx_delivery_reports_installation
    ON t_delivery_reports(installation_id);

COMMENT ON TABLE t_solution_packages IS '已完整校验、内容摘要不可变的解决方案包';
COMMENT ON TABLE t_solution_install_plans IS '锁定包摘要和基础站点版本的可审查安装计划';
COMMENT ON TABLE t_delivery_reports IS '由公开验收边界生成的不可变机器报告';
