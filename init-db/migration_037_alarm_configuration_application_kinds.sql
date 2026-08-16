-- Alarm configuration applications have one versioned lineage, including
-- reviewed legacy migration plans. Existing unversioned migrated evidence is
-- rejected because its application/version ancestry cannot be reconstructed.
BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM t_legacy_alarm_migrations
        WHERE state = 'migrated'
          AND NOT (details ? 'plan_id')
    ) THEN
        RAISE EXCEPTION
            'legacy alarm migration evidence has no versioned application lineage';
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS trg_alarm_configuration_plans_append_only
    ON t_alarm_configuration_plans;

ALTER TABLE t_alarm_configuration_plans
    ADD COLUMN IF NOT EXISTS plan_kind TEXT NOT NULL DEFAULT 'rule_set';
ALTER TABLE t_alarm_configuration_plans
    ALTER COLUMN rule_set_id DROP NOT NULL,
    ALTER COLUMN rule_set_revision DROP NOT NULL,
    DROP CONSTRAINT IF EXISTS chk_alarm_configuration_plan_kind,
    ADD CONSTRAINT chk_alarm_configuration_plan_kind CHECK (
        (
            plan_kind = 'rule_set'
            AND rule_set_id IS NOT NULL
            AND rule_set_revision IS NOT NULL
        )
        OR
        (
            plan_kind = 'legacy_migration'
            AND rule_set_id IS NULL
            AND rule_set_revision IS NULL
        )
    );

DROP TRIGGER IF EXISTS trg_legacy_alarm_migrations_append_only
    ON t_legacy_alarm_migrations;
ALTER TABLE t_legacy_alarm_migrations
    ADD COLUMN IF NOT EXISTS plan_id UUID
        REFERENCES t_alarm_configuration_plans(id);
UPDATE t_legacy_alarm_migrations
SET plan_id = (details->>'plan_id')::uuid
WHERE state = 'migrated'
  AND plan_id IS NULL
  AND details ? 'plan_id';
ALTER TABLE t_legacy_alarm_migrations
    DROP CONSTRAINT IF EXISTS chk_legacy_alarm_migration_plan,
    ADD CONSTRAINT chk_legacy_alarm_migration_plan CHECK (
        state <> 'migrated' OR plan_id IS NOT NULL
    );
CREATE TRIGGER trg_legacy_alarm_migrations_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_legacy_alarm_migrations
    FOR EACH STATEMENT EXECUTE FUNCTION reject_legacy_alarm_migration_mutation();

ALTER TABLE t_alarm_definition_origins
    DROP CONSTRAINT IF EXISTS chk_alarm_definition_origin_rule_set,
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
            origin_type = 'package'
            AND rule_set_id IS NULL
            AND rule_set_revision IS NULL
            AND plan_id IS NULL
        )
        OR
        (
            origin_type = 'legacy_migration'
            AND rule_set_id IS NULL
            AND rule_set_revision IS NULL
            AND plan_id IS NOT NULL
        )
    );

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM t_alarm_definition_origins origin
        JOIN t_alarm_configuration_plans plan ON plan.id = origin.plan_id
        WHERE origin.origin_type = 'legacy_migration'
          AND plan.plan_kind <> 'legacy_migration'
    ) OR EXISTS (
        SELECT 1
        FROM t_legacy_alarm_migrations migration
        JOIN t_alarm_configuration_plans plan ON plan.id = migration.plan_id
        WHERE migration.state = 'migrated'
          AND plan.plan_kind <> 'legacy_migration'
    ) THEN
        RAISE EXCEPTION
            'legacy alarm evidence requires a legacy_migration plan';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION enforce_legacy_alarm_evidence_plan_kind()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    linked_plan_kind TEXT;
BEGIN
    IF TG_TABLE_NAME = 't_alarm_definition_origins' THEN
        IF NEW.origin_type <> 'legacy_migration' THEN
            RETURN NEW;
        END IF;
    ELSIF TG_TABLE_NAME = 't_legacy_alarm_migrations' THEN
        IF NEW.state <> 'migrated' THEN
            RETURN NEW;
        END IF;
    END IF;
    SELECT plan_kind INTO linked_plan_kind
    FROM t_alarm_configuration_plans
    WHERE id = NEW.plan_id;
    IF linked_plan_kind IS DISTINCT FROM 'legacy_migration' THEN
        RAISE EXCEPTION
            'legacy alarm evidence requires a legacy_migration plan';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_alarm_definition_origin_legacy_plan_kind
    ON t_alarm_definition_origins;
CREATE TRIGGER trg_alarm_definition_origin_legacy_plan_kind
    BEFORE INSERT OR UPDATE ON t_alarm_definition_origins
    FOR EACH ROW EXECUTE FUNCTION enforce_legacy_alarm_evidence_plan_kind();

DROP TRIGGER IF EXISTS trg_legacy_alarm_migration_plan_kind
    ON t_legacy_alarm_migrations;
CREATE TRIGGER trg_legacy_alarm_migration_plan_kind
    BEFORE INSERT OR UPDATE ON t_legacy_alarm_migrations
    FOR EACH ROW EXECUTE FUNCTION enforce_legacy_alarm_evidence_plan_kind();

CREATE OR REPLACE FUNCTION enforce_alarm_configuration_plan_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.status = 'ready'
       AND NEW.status = 'applied'
       AND NEW.id = OLD.id
       AND NEW.source_installation_id = OLD.source_installation_id
       AND NEW.base_site_configuration_version = OLD.base_site_configuration_version
       AND NEW.plan_kind = OLD.plan_kind
       AND NEW.rule_set_id IS NOT DISTINCT FROM OLD.rule_set_id
       AND NEW.rule_set_revision IS NOT DISTINCT FROM OLD.rule_set_revision
       AND NEW.planned_by = OLD.planned_by
       AND NEW.canonical_plan = OLD.canonical_plan
       AND NEW.digest = OLD.digest
       AND NEW.created_at = OLD.created_at THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'applied alarm configuration plans are append-only';
END;
$$;

CREATE TRIGGER trg_alarm_configuration_plans_append_only
    BEFORE UPDATE OR DELETE ON t_alarm_configuration_plans
    FOR EACH ROW EXECUTE FUNCTION enforce_alarm_configuration_plan_append_only();

COMMENT ON COLUMN t_alarm_configuration_plans.plan_kind IS
    'Reviewed rule_set or legacy_migration application kind';
COMMENT ON COLUMN t_legacy_alarm_migrations.plan_id IS
    'Versioned alarm configuration plan that installed this legacy source';

COMMIT;
