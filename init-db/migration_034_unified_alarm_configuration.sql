-- Unified alarm configuration: immutable rule revisions, reviewable plans,
-- definition provenance and one-transaction apply evidence.
BEGIN;

CREATE TABLE IF NOT EXISTS t_alarm_rule_sets (
    id UUID PRIMARY KEY,
    rule_set_key TEXT NOT NULL,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_alarm_rule_set_key_nonempty
        CHECK (length(btrim(rule_set_key)) > 0),
    CONSTRAINT chk_alarm_rule_set_name_nonempty
        CHECK (length(btrim(name)) > 0),
    CONSTRAINT uq_alarm_rule_sets_key UNIQUE (rule_set_key)
);

CREATE TABLE IF NOT EXISTS t_alarm_rule_set_revisions (
    rule_set_id UUID NOT NULL REFERENCES t_alarm_rule_sets(id),
    revision INTEGER NOT NULL,
    rules JSONB NOT NULL,
    digest CHAR(64) NOT NULL,
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_alarm_rule_set_revisions
        PRIMARY KEY (rule_set_id, revision),
    CONSTRAINT chk_alarm_rule_set_revision_positive
        CHECK (revision > 0),
    CONSTRAINT chk_alarm_rule_set_revision_rules_array
        CHECK (jsonb_typeof(rules) = 'array'),
    CONSTRAINT chk_alarm_rule_set_revision_digest_sha256
        CHECK (digest ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS t_alarm_configuration_plans (
    id UUID PRIMARY KEY,
    source_installation_id UUID NOT NULL REFERENCES t_solution_installations(id),
    base_site_configuration_version BIGINT NOT NULL
        REFERENCES t_site_configuration_versions(version),
    rule_set_id UUID NOT NULL,
    rule_set_revision INTEGER NOT NULL,
    canonical_plan JSONB NOT NULL,
    digest CHAR(64) NOT NULL,
    status TEXT NOT NULL,
    actor TEXT,
    applied_result JSONB,
    applied_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_alarm_configuration_plan_rule_revision
        FOREIGN KEY (rule_set_id, rule_set_revision)
        REFERENCES t_alarm_rule_set_revisions(rule_set_id, revision),
    CONSTRAINT uq_alarm_configuration_plan_digest UNIQUE (digest),
    CONSTRAINT chk_alarm_configuration_plan_digest_sha256
        CHECK (digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_alarm_configuration_plan_status
        CHECK (status IN ('ready', 'blocked', 'applied')),
    CONSTRAINT chk_alarm_configuration_plan_applied_result CHECK (
        (status = 'applied' AND actor IS NOT NULL
         AND applied_result IS NOT NULL AND applied_at IS NOT NULL)
        OR
        (status <> 'applied' AND actor IS NULL
         AND applied_result IS NULL AND applied_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS t_alarm_definition_origins (
    definition_id UUID PRIMARY KEY REFERENCES t_alarm_definitions(id),
    origin_type TEXT NOT NULL,
    rule_set_id UUID,
    rule_set_revision INTEGER,
    plan_id UUID REFERENCES t_alarm_configuration_plans(id),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_alarm_definition_origin_rule_revision
        FOREIGN KEY (rule_set_id, rule_set_revision)
        REFERENCES t_alarm_rule_set_revisions(rule_set_id, revision),
    CONSTRAINT chk_alarm_definition_origin_type
        CHECK (origin_type IN ('package', 'rule_set', 'site_override', 'legacy_migration')),
    CONSTRAINT chk_alarm_definition_origin_rule_set CHECK (
        origin_type <> 'rule_set'
        OR (rule_set_id IS NOT NULL AND rule_set_revision IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS t_legacy_alarm_migrations (
    id UUID PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_key TEXT NOT NULL,
    target_definition_ids UUID[] NOT NULL,
    state TEXT NOT NULL,
    actor TEXT NOT NULL,
    migrated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_legacy_alarm_migration_source
        UNIQUE (source_kind, source_key),
    CONSTRAINT chk_legacy_alarm_migration_source_kind
        CHECK (length(btrim(source_kind)) > 0),
    CONSTRAINT chk_legacy_alarm_migration_source_key
        CHECK (length(btrim(source_key)) > 0),
    CONSTRAINT chk_legacy_alarm_migration_targets
        CHECK (cardinality(target_definition_ids) > 0),
    CONSTRAINT chk_legacy_alarm_migration_state
        CHECK (state IN ('previewed', 'migrated', 'rejected'))
);

CREATE TABLE IF NOT EXISTS t_alarm_configuration_idempotency (
    actor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest CHAR(64) NOT NULL,
    plan_id UUID NOT NULL REFERENCES t_alarm_configuration_plans(id),
    applied_installation_id UUID NOT NULL REFERENCES t_solution_installations(id),
    applied_result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_alarm_configuration_idempotency
        PRIMARY KEY (actor, idempotency_key),
    CONSTRAINT chk_alarm_configuration_idempotency_key
        CHECK (length(btrim(idempotency_key)) > 0),
    CONSTRAINT chk_alarm_configuration_idempotency_digest_sha256
        CHECK (request_digest ~ '^[0-9a-f]{64}$')
);

ALTER TABLE t_alarm_definitions
    DROP CONSTRAINT IF EXISTS
        t_alarm_definitions_installation_id_asset_id_entity_instance_id_key;
ALTER TABLE t_alarm_definitions
    DROP CONSTRAINT IF EXISTS
        t_alarm_definitions_trigger_duration_seconds_check,
    DROP CONSTRAINT IF EXISTS
        t_alarm_definitions_recovery_duration_seconds_check,
    DROP CONSTRAINT IF EXISTS
        t_alarm_definitions_notification_throttle_seconds_check,
    DROP CONSTRAINT IF EXISTS
        chk_alarm_definition_trigger_duration_nonnegative,
    DROP CONSTRAINT IF EXISTS
        chk_alarm_definition_recovery_duration_nonnegative,
    DROP CONSTRAINT IF EXISTS
        chk_alarm_definition_throttle_nonnegative;
ALTER TABLE t_alarm_definitions
    ADD CONSTRAINT chk_alarm_definition_trigger_duration_nonnegative
        CHECK (trigger_duration_seconds >= 0),
    ADD CONSTRAINT chk_alarm_definition_recovery_duration_nonnegative
        CHECK (recovery_duration_seconds >= 0),
    ADD CONSTRAINT chk_alarm_definition_throttle_nonnegative
        CHECK (notification_throttle_seconds >= 0);
ALTER TABLE t_alarm_definitions
    DROP CONSTRAINT IF EXISTS
        uq_alarm_definitions_installation_asset_entity_digest;
ALTER TABLE t_alarm_definitions
    ADD CONSTRAINT uq_alarm_definitions_installation_asset_entity_digest
    UNIQUE (installation_id, asset_id, entity_instance_id, content_digest);

CREATE OR REPLACE FUNCTION reject_alarm_rule_set_revision_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'alarm rule-set revisions are immutable';
END;
$$;

DROP TRIGGER IF EXISTS trg_alarm_rule_set_revisions_immutable
    ON t_alarm_rule_set_revisions;
CREATE TRIGGER trg_alarm_rule_set_revisions_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_alarm_rule_set_revisions
    FOR EACH STATEMENT EXECUTE FUNCTION reject_alarm_rule_set_revision_mutation();

CREATE OR REPLACE FUNCTION enforce_alarm_configuration_plan_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.status = 'ready'
       AND NEW.status = 'applied'
       AND NEW.id = OLD.id
       AND NEW.source_installation_id = OLD.source_installation_id
       AND NEW.base_site_configuration_version = OLD.base_site_configuration_version
       AND NEW.rule_set_id = OLD.rule_set_id
       AND NEW.rule_set_revision = OLD.rule_set_revision
       AND NEW.canonical_plan = OLD.canonical_plan
       AND NEW.digest = OLD.digest
       AND NEW.created_at = OLD.created_at THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'applied alarm configuration plans are append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_alarm_configuration_plans_append_only
    ON t_alarm_configuration_plans;
DROP TRIGGER IF EXISTS trg_alarm_configuration_plans_truncate
    ON t_alarm_configuration_plans;
CREATE TRIGGER trg_alarm_configuration_plans_append_only
    BEFORE UPDATE OR DELETE ON t_alarm_configuration_plans
    FOR EACH ROW EXECUTE FUNCTION enforce_alarm_configuration_plan_append_only();
CREATE TRIGGER trg_alarm_configuration_plans_truncate
    BEFORE TRUNCATE ON t_alarm_configuration_plans
    FOR EACH STATEMENT EXECUTE FUNCTION enforce_alarm_configuration_plan_append_only();

CREATE OR REPLACE FUNCTION reject_alarm_definition_origin_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'alarm definition origins are append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_alarm_definition_origins_append_only
    ON t_alarm_definition_origins;
CREATE TRIGGER trg_alarm_definition_origins_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_alarm_definition_origins
    FOR EACH STATEMENT EXECUTE FUNCTION reject_alarm_definition_origin_mutation();

CREATE OR REPLACE FUNCTION reject_legacy_alarm_migration_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'legacy alarm migration evidence is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_legacy_alarm_migrations_append_only
    ON t_legacy_alarm_migrations;
CREATE TRIGGER trg_legacy_alarm_migrations_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_legacy_alarm_migrations
    FOR EACH STATEMENT EXECUTE FUNCTION reject_legacy_alarm_migration_mutation();

COMMENT ON TABLE t_alarm_configuration_plans IS
    'Complete canonical alarm plans; ready plans become immutable applied evidence';
COMMENT ON TABLE t_legacy_alarm_migrations IS
    'Append-only migration evidence; legacy alarm rows remain untouched';

COMMIT;
