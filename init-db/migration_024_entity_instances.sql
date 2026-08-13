-- Migration 024: stable device/entity instances and deterministic primary binding.

BEGIN;

DROP TRIGGER IF EXISTS trg_site_configuration_versions_append_only
ON t_site_configuration_versions;

ALTER TABLE t_nodes
    ADD COLUMN IF NOT EXISTS source_catalog_key TEXT;

WITH unique_names AS (
    SELECT name FROM t_nodes GROUP BY name HAVING count(*) = 1
)
UPDATE t_nodes n
SET source_catalog_key = n.name
FROM unique_names u
WHERE n.name = u.name AND n.source_catalog_key IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_nodes_source_catalog_key
ON t_nodes(source_catalog_key) WHERE source_catalog_key IS NOT NULL;

ALTER TABLE t_solution_install_plans
    ADD COLUMN IF NOT EXISTS target_installation_id UUID,
    ADD COLUMN IF NOT EXISTS entity_identity_installation_id UUID,
    ADD COLUMN IF NOT EXISTS entity_plan JSONB;

UPDATE t_solution_install_plans
SET target_installation_id = id,
    entity_identity_installation_id = id
WHERE target_installation_id IS NULL
   OR entity_identity_installation_id IS NULL;

ALTER TABLE t_solution_install_plans
    ALTER COLUMN target_installation_id SET NOT NULL,
    ALTER COLUMN entity_identity_installation_id SET NOT NULL;

ALTER TABLE t_site_configuration_versions
    ADD COLUMN IF NOT EXISTS entity_identity_installation_id UUID;

UPDATE t_site_configuration_versions
SET entity_identity_installation_id = installation_id
WHERE entity_identity_installation_id IS NULL
  AND installation_id IS NOT NULL;

ALTER TABLE t_site_configuration_versions
    DROP CONSTRAINT IF EXISTS chk_site_configuration_entity_identity;
ALTER TABLE t_site_configuration_versions
    ADD CONSTRAINT chk_site_configuration_entity_identity CHECK (
        installation_id IS NULL OR entity_identity_installation_id IS NOT NULL
    );

ALTER TABLE t_solution_installations
    ADD COLUMN IF NOT EXISTS entity_instance_ids UUID[] NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS t_device_instances (
    id UUID PRIMARY KEY,
    identity_installation_id UUID NOT NULL,
    slot_id TEXT NOT NULL,
    instance_key TEXT NOT NULL,
    device_category TEXT NOT NULL,
    display_name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (identity_installation_id, slot_id, instance_key)
);

CREATE TABLE IF NOT EXISTS t_entity_instances (
    id UUID PRIMARY KEY,
    device_instance_id UUID NOT NULL REFERENCES t_device_instances(id),
    definition_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    data_type TEXT NOT NULL CHECK (data_type IN ('FLOAT', 'INT', 'BOOL', 'STRING', 'ENUM')),
    unit TEXT,
    direction TEXT NOT NULL CHECK (direction IN ('R', 'W', 'RW')),
    freshness_seconds DOUBLE PRECISION NOT NULL CHECK (freshness_seconds > 0),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (device_instance_id, definition_id)
);

CREATE TABLE IF NOT EXISTS t_entity_binding_confirmations (
    id UUID PRIMARY KEY,
    entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
    binding_id UUID NOT NULL,
    actor TEXT NOT NULL,
    matcher_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    plan_digest CHAR(64) NOT NULL,
    selected_tag_id UUID NOT NULL REFERENCES t_tags(id),
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t_entity_instance_bindings (
    id UUID PRIMARY KEY,
    entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
    tag_id UUID NOT NULL REFERENCES t_tags(id),
    matcher_id TEXT NOT NULL,
    confirmation_audit_id UUID NOT NULL
      REFERENCES t_entity_binding_confirmations(id) DEFERRABLE INITIALLY DEFERRED,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_instance_active_primary
ON t_entity_instance_bindings(entity_instance_id) WHERE active = TRUE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_tag_active_primary
ON t_entity_instance_bindings(tag_id) WHERE active = TRUE;

CREATE INDEX IF NOT EXISTS idx_entity_instances_device
ON t_entity_instances(device_instance_id);

CREATE OR REPLACE FUNCTION reject_entity_confirmation_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'entity binding confirmations are append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_entity_confirmation_append_only
ON t_entity_binding_confirmations;
CREATE TRIGGER trg_entity_confirmation_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON t_entity_binding_confirmations
FOR EACH STATEMENT EXECUTE FUNCTION reject_entity_confirmation_mutation();

CREATE TRIGGER trg_site_configuration_versions_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_site_configuration_versions
    FOR EACH STATEMENT
    EXECUTE FUNCTION reject_site_configuration_version_mutation();

COMMIT;
