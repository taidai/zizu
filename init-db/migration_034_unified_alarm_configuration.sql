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
    CONSTRAINT chk_alarm_rule_set_created_by_nonempty
        CHECK (length(btrim(created_by)) > 0),
    CONSTRAINT uq_alarm_rule_sets_key UNIQUE (rule_set_key)
);
ALTER TABLE t_alarm_rule_sets
    DROP CONSTRAINT IF EXISTS chk_alarm_rule_set_created_by_nonempty,
    ADD CONSTRAINT chk_alarm_rule_set_created_by_nonempty
        CHECK (length(btrim(created_by)) > 0);

CREATE TABLE IF NOT EXISTS t_alarm_rule_set_revisions (
    rule_set_id UUID NOT NULL REFERENCES t_alarm_rule_sets(id),
    revision INTEGER NOT NULL,
    rule_set_key TEXT,
    rule_set_name TEXT,
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
        CHECK (digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_alarm_rule_set_revision_actor_nonempty
        CHECK (length(btrim(actor)) > 0)
);

DROP TRIGGER IF EXISTS trg_alarm_rule_set_revisions_immutable
    ON t_alarm_rule_set_revisions;
ALTER TABLE t_alarm_rule_set_revisions
    ADD COLUMN IF NOT EXISTS rule_set_key TEXT,
    ADD COLUMN IF NOT EXISTS rule_set_name TEXT;
UPDATE t_alarm_rule_set_revisions revision
SET rule_set_key = rule_set.rule_set_key,
    rule_set_name = rule_set.name
FROM t_alarm_rule_sets rule_set
WHERE rule_set.id = revision.rule_set_id
  AND (revision.rule_set_key IS NULL OR revision.rule_set_name IS NULL);
ALTER TABLE t_alarm_rule_set_revisions
    DROP CONSTRAINT IF EXISTS chk_alarm_rule_set_revision_key_nonempty,
    DROP CONSTRAINT IF EXISTS chk_alarm_rule_set_revision_name_nonempty,
    ALTER COLUMN rule_set_key SET NOT NULL,
    ALTER COLUMN rule_set_name SET NOT NULL,
    ADD CONSTRAINT chk_alarm_rule_set_revision_key_nonempty
        CHECK (length(btrim(rule_set_key)) > 0),
    ADD CONSTRAINT chk_alarm_rule_set_revision_name_nonempty
        CHECK (length(btrim(rule_set_name)) > 0);
ALTER TABLE t_alarm_rule_set_revisions
    DROP CONSTRAINT IF EXISTS chk_alarm_rule_set_revision_actor_nonempty,
    ADD CONSTRAINT chk_alarm_rule_set_revision_actor_nonempty
        CHECK (length(btrim(actor)) > 0);

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
    planned_by TEXT,
    applied_by TEXT,
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
    CONSTRAINT chk_alarm_configuration_plan_planned_by_nonempty
        CHECK (length(btrim(planned_by)) > 0),
    CONSTRAINT chk_alarm_configuration_plan_applied_by_nonempty
        CHECK (applied_by IS NULL OR length(btrim(applied_by)) > 0),
    CONSTRAINT chk_alarm_configuration_plan_applied_result CHECK (
        (status = 'applied' AND applied_by IS NOT NULL
         AND applied_result IS NOT NULL AND applied_at IS NOT NULL)
        OR
        (status <> 'applied' AND applied_by IS NULL
         AND applied_result IS NULL AND applied_at IS NULL)
    )
);

-- A revised migration_034 can be replayed safely on databases that briefly ran
-- the original actor-only plan schema. Drop lifecycle triggers before the
-- one-time metadata backfill; canonical plan JSON remains byte-for-byte stable
-- after this migration finishes.
DROP TRIGGER IF EXISTS trg_alarm_configuration_plans_append_only
    ON t_alarm_configuration_plans;
DROP TRIGGER IF EXISTS trg_alarm_configuration_plans_truncate
    ON t_alarm_configuration_plans;
ALTER TABLE t_alarm_configuration_plans
    ADD COLUMN IF NOT EXISTS planned_by TEXT,
    ADD COLUMN IF NOT EXISTS applied_by TEXT;
ALTER TABLE t_alarm_configuration_plans
    DROP CONSTRAINT IF EXISTS chk_alarm_configuration_plan_applied_result,
    DROP CONSTRAINT IF EXISTS chk_alarm_configuration_plan_planned_by_nonempty,
    DROP CONSTRAINT IF EXISTS chk_alarm_configuration_plan_applied_by_nonempty;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 't_alarm_configuration_plans'
          AND column_name = 'actor'
    ) THEN
        EXECUTE $sql$
            UPDATE t_alarm_configuration_plans
            SET planned_by = COALESCE(
                    NULLIF(btrim(canonical_plan->>'planned_by'), ''),
                    NULLIF(btrim(actor), ''),
                    'system:legacy-alarm-planner'
                ),
                applied_by = CASE WHEN status = 'applied' THEN COALESCE(
                    NULLIF(btrim(actor), ''),
                    'system:legacy-alarm-applier'
                ) ELSE NULL END
        $sql$;
        EXECUTE 'ALTER TABLE t_alarm_configuration_plans DROP COLUMN actor';
    ELSE
        UPDATE t_alarm_configuration_plans
        SET planned_by = COALESCE(
                NULLIF(btrim(canonical_plan->>'planned_by'), ''),
                'system:legacy-alarm-planner'
            )
        WHERE planned_by IS NULL OR btrim(planned_by) = '';
    END IF;
END;
$$;
ALTER TABLE t_alarm_configuration_plans
    ALTER COLUMN planned_by SET NOT NULL,
    ADD CONSTRAINT chk_alarm_configuration_plan_planned_by_nonempty
        CHECK (length(btrim(planned_by)) > 0),
    ADD CONSTRAINT chk_alarm_configuration_plan_applied_by_nonempty
        CHECK (applied_by IS NULL OR length(btrim(applied_by)) > 0),
    ADD CONSTRAINT chk_alarm_configuration_plan_applied_result CHECK (
        (status = 'applied' AND applied_by IS NOT NULL
         AND applied_result IS NOT NULL AND applied_at IS NOT NULL)
        OR
        (status <> 'applied' AND applied_by IS NULL
         AND applied_result IS NULL AND applied_at IS NULL)
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
        (
            origin_type = 'rule_set'
            AND rule_set_id IS NOT NULL
            AND rule_set_revision IS NOT NULL
            AND plan_id IS NOT NULL
        )
        OR
        (
            origin_type = 'site_override'
            AND rule_set_id IS NULL
            AND rule_set_revision IS NULL
            AND plan_id IS NOT NULL
        )
        OR
        (
            origin_type IN ('package', 'legacy_migration')
            AND rule_set_id IS NULL
            AND rule_set_revision IS NULL
            AND plan_id IS NULL
        )
    ),
    CONSTRAINT chk_alarm_definition_origin_actor_nonempty
        CHECK (length(btrim(actor)) > 0)
);
ALTER TABLE t_alarm_definition_origins
    DROP CONSTRAINT IF EXISTS chk_alarm_definition_origin_rule_set,
    DROP CONSTRAINT IF EXISTS chk_alarm_definition_origin_actor_nonempty,
    ADD CONSTRAINT chk_alarm_definition_origin_rule_set CHECK (
        (
            origin_type = 'rule_set'
            AND rule_set_id IS NOT NULL
            AND rule_set_revision IS NOT NULL
            AND plan_id IS NOT NULL
        )
        OR
        (
            origin_type = 'site_override'
            AND rule_set_id IS NULL
            AND rule_set_revision IS NULL
            AND plan_id IS NOT NULL
        )
        OR
        (
            origin_type IN ('package', 'legacy_migration')
            AND rule_set_id IS NULL
            AND rule_set_revision IS NULL
            AND plan_id IS NULL
        )
    ),
    ADD CONSTRAINT chk_alarm_definition_origin_actor_nonempty
        CHECK (length(btrim(actor)) > 0);

CREATE TABLE IF NOT EXISTS t_legacy_alarm_migrations (
    id UUID PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_key TEXT NOT NULL,
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
    CONSTRAINT chk_legacy_alarm_migration_state
        CHECK (state IN ('previewed', 'migrated', 'rejected')),
    CONSTRAINT chk_legacy_alarm_migration_actor_nonempty
        CHECK (length(btrim(actor)) > 0)
);
ALTER TABLE t_legacy_alarm_migrations
    DROP CONSTRAINT IF EXISTS chk_legacy_alarm_migration_actor_nonempty,
    ADD CONSTRAINT chk_legacy_alarm_migration_actor_nonempty
        CHECK (length(btrim(actor)) > 0);

CREATE TABLE IF NOT EXISTS t_legacy_alarm_migration_targets (
    migration_id UUID NOT NULL REFERENCES t_legacy_alarm_migrations(id),
    definition_id UUID NOT NULL REFERENCES t_alarm_definitions(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_legacy_alarm_migration_targets
        PRIMARY KEY (migration_id, definition_id)
);
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 't_legacy_alarm_migrations'
          AND column_name = 'target_definition_ids'
    ) THEN
        EXECUTE $sql$
            INSERT INTO t_legacy_alarm_migration_targets
              (migration_id, definition_id)
            SELECT migration.id, target.definition_id
            FROM t_legacy_alarm_migrations migration
            CROSS JOIN LATERAL unnest(migration.target_definition_ids)
              AS target(definition_id)
            ON CONFLICT (migration_id, definition_id) DO NOTHING
        $sql$;
        EXECUTE $sql$
            ALTER TABLE t_legacy_alarm_migrations
            DROP COLUMN target_definition_ids
        $sql$;
    END IF;
END;
$$;

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
        CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_alarm_configuration_idempotency_actor_nonempty
        CHECK (length(btrim(actor)) > 0)
);
ALTER TABLE t_alarm_configuration_idempotency
    DROP CONSTRAINT IF EXISTS chk_alarm_configuration_idempotency_actor_nonempty,
    ADD CONSTRAINT chk_alarm_configuration_idempotency_actor_nonempty
        CHECK (length(btrim(actor)) > 0);

DROP TRIGGER IF EXISTS trg_alarm_definitions_immutable
    ON t_alarm_definitions;
ALTER TABLE t_alarm_definitions
    DROP CONSTRAINT IF EXISTS
        t_alarm_definitions_installation_id_asset_id_entity_instance_id_key,
    DROP CONSTRAINT IF EXISTS
        t_alarm_definitions_installation_id_asset_id_entity_instanc_key;
ALTER TABLE t_alarm_definitions
    ADD COLUMN IF NOT EXISTS content_digest_algorithm TEXT;
UPDATE t_alarm_definitions
SET content_digest_algorithm = 'legacy-unknown'
WHERE content_digest_algorithm IS NULL;
ALTER TABLE t_alarm_definitions
    ALTER COLUMN content_digest_algorithm
        SET DEFAULT 'sha256-v2-content',
    ALTER COLUMN content_digest_algorithm SET NOT NULL,
    DROP CONSTRAINT IF EXISTS chk_alarm_definition_digest_algorithm,
    ADD CONSTRAINT chk_alarm_definition_digest_algorithm CHECK (
        content_digest_algorithm IN (
            'legacy-unknown',
            'sha256-v1-installation-bound',
            'sha256-v2-content'
        )
    );
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
    UNIQUE (
        installation_id, asset_id, entity_instance_id,
        content_digest_algorithm, content_digest
    );
CREATE TRIGGER trg_alarm_definitions_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_alarm_definitions
    FOR EACH STATEMENT EXECUTE FUNCTION reject_alarm_definition_mutation();

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
       AND NEW.planned_by = OLD.planned_by
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

DROP TRIGGER IF EXISTS trg_legacy_alarm_migration_targets_append_only
    ON t_legacy_alarm_migration_targets;
CREATE TRIGGER trg_legacy_alarm_migration_targets_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_legacy_alarm_migration_targets
    FOR EACH STATEMENT EXECUTE FUNCTION reject_legacy_alarm_migration_mutation();

CREATE OR REPLACE FUNCTION require_legacy_alarm_migration_target()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM t_legacy_alarm_migration_targets target
        WHERE target.migration_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'legacy alarm migration requires an existing definition target';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_legacy_alarm_migration_requires_target
    ON t_legacy_alarm_migrations;
CREATE CONSTRAINT TRIGGER trg_legacy_alarm_migration_requires_target
    AFTER INSERT ON t_legacy_alarm_migrations
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION require_legacy_alarm_migration_target();

CREATE OR REPLACE FUNCTION reject_legacy_alarm_configuration_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'legacy alarm configuration is read-only; use alarm configuration migration';
END;
$$;

DROP TRIGGER IF EXISTS trg_alarm_levels_read_only ON t_alarm_levels;
CREATE TRIGGER trg_alarm_levels_read_only
    BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON t_alarm_levels
    FOR EACH STATEMENT EXECUTE FUNCTION reject_legacy_alarm_configuration_mutation();

DROP TRIGGER IF EXISTS trg_entity_alarm_bindings_read_only
    ON t_entity_alarm_bindings;
CREATE TRIGGER trg_entity_alarm_bindings_read_only
    BEFORE INSERT OR UPDATE OR DELETE OR TRUNCATE ON t_entity_alarm_bindings
    FOR EACH STATEMENT EXECUTE FUNCTION reject_legacy_alarm_configuration_mutation();

CREATE OR REPLACE FUNCTION reject_new_legacy_tag_alarm_configuration()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.alarm_level IS NOT NULL
       OR NEW.alarm_type IS NOT NULL
       OR NEW.alarm_threshold IS NOT NULL
       OR NEW.fault_map_id IS NOT NULL THEN
        RAISE EXCEPTION 'legacy tag alarm configuration is read-only; use alarm configuration migration';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_tags_reject_legacy_alarm_insert ON t_tags;
CREATE TRIGGER trg_tags_reject_legacy_alarm_insert
    BEFORE INSERT ON t_tags
    FOR EACH ROW EXECUTE FUNCTION reject_new_legacy_tag_alarm_configuration();

DROP TRIGGER IF EXISTS trg_tags_legacy_alarm_columns_read_only ON t_tags;
CREATE TRIGGER trg_tags_legacy_alarm_columns_read_only
    BEFORE UPDATE OF alarm_level, alarm_type, alarm_threshold, fault_map_id
    ON t_tags
    FOR EACH STATEMENT EXECUTE FUNCTION reject_legacy_alarm_configuration_mutation();

COMMENT ON TABLE t_alarm_configuration_plans IS
    'Complete canonical alarm plans; ready plans become immutable applied evidence';
COMMENT ON TABLE t_legacy_alarm_migrations IS
    'Append-only migration evidence; legacy alarm rows remain untouched';

COMMIT;
