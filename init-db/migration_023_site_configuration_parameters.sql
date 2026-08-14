-- Migration 023: typed site parameters and immutable Secret references.

BEGIN;

DROP TRIGGER IF EXISTS trg_site_configuration_versions_append_only
    ON t_site_configuration_versions;

ALTER TABLE t_solution_install_plans
    ADD COLUMN IF NOT EXISTS parameter_contracts JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS secret_references JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS parameter_sources JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS parameter_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS configuration_digest CHAR(64)
        NOT NULL DEFAULT repeat('0', 64);

ALTER TABLE t_site_configuration_versions
    ADD COLUMN IF NOT EXISTS parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS secret_references JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS parameter_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS configuration_digest CHAR(64)
        NOT NULL DEFAULT repeat('0', 64);

-- Prior releases used the immutable package digest as the complete identity of
-- parameterless site configuration. Backfill that identity so upgrading does
-- not turn an unchanged package into a spurious configuration update.
UPDATE t_solution_install_plans
SET configuration_digest = package_digest
WHERE configuration_digest = repeat('0', 64);

UPDATE t_site_configuration_versions
SET configuration_digest = package_digest
WHERE installation_id IS NOT NULL
  AND configuration_digest = repeat('0', 64);

ALTER TABLE t_solution_install_plans
    DROP CONSTRAINT IF EXISTS chk_solution_plan_configuration_digest;
ALTER TABLE t_solution_install_plans
    ADD CONSTRAINT chk_solution_plan_configuration_digest
    CHECK (configuration_digest ~ '^[0-9a-f]{64}$');

ALTER TABLE t_site_configuration_versions
    DROP CONSTRAINT IF EXISTS chk_site_configuration_digest;
ALTER TABLE t_site_configuration_versions
    ADD CONSTRAINT chk_site_configuration_digest
    CHECK (configuration_digest ~ '^[0-9a-f]{64}$');

CREATE INDEX IF NOT EXISTS idx_site_configuration_digest
    ON t_site_configuration_versions(package_record_id, package_digest,
                                     configuration_digest)
    WHERE installation_id IS NOT NULL;

CREATE OR REPLACE FUNCTION reject_site_configuration_version_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 't_site_configuration_versions is append-only';
END;
$$;

CREATE TRIGGER trg_site_configuration_versions_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_site_configuration_versions
    FOR EACH STATEMENT
    EXECUTE FUNCTION reject_site_configuration_version_mutation();

COMMENT ON COLUMN t_site_configuration_versions.secret_references IS
    'Opaque secret:// references only; raw Secret values are forbidden';

COMMIT;
