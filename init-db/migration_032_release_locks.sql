-- Deployment-owned immutable evidence for a successful platform switch.
CREATE TABLE IF NOT EXISTS t_release_locks (
    id UUID PRIMARY KEY,
    platform_version TEXT NOT NULL CHECK (length(trim(platform_version)) > 0),
    platform_image TEXT NOT NULL CHECK (platform_image ~ '@sha256:[0-9a-f]{64}$'),
    platform_image_id TEXT NOT NULL CHECK (platform_image_id ~ '^sha256:[0-9a-f]{64}$'),
    edge_proxy_image TEXT NOT NULL CHECK (edge_proxy_image ~ '@sha256:[0-9a-f]{64}$'),
    edge_proxy_image_id TEXT NOT NULL CHECK (edge_proxy_image_id ~ '^sha256:[0-9a-f]{64}$'),
    architecture TEXT NOT NULL CHECK (architecture IN ('linux/amd64', 'linux/arm64')),
    schema_version TEXT NOT NULL CHECK (schema_version ~ '^[0-9]+$'),
    site_configuration_version BIGINT NOT NULL
        REFERENCES t_site_configuration_versions(version),
    package_id TEXT NULL,
    package_version TEXT NULL,
    package_digest CHAR(64) NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (site_configuration_version = 0 AND package_id IS NULL
         AND package_version IS NULL AND package_digest IS NULL)
        OR
        (site_configuration_version > 0 AND package_id IS NOT NULL
         AND package_version IS NOT NULL AND package_digest ~ '^[0-9a-f]{64}$')
    )
);

CREATE INDEX IF NOT EXISTS idx_release_locks_generated_at
    ON t_release_locks (generated_at DESC, id DESC);

CREATE OR REPLACE FUNCTION reject_release_lock_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 't_release_locks is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_release_locks_append_only ON t_release_locks;
CREATE TRIGGER trg_release_locks_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_release_locks
    FOR EACH STATEMENT
    EXECUTE FUNCTION reject_release_lock_mutation();

COMMENT ON TABLE t_release_locks IS
    'Owner-job deployment evidence: immutable image digests, schema and installed site configuration';
