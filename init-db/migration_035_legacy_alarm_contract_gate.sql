-- Task 5 contract migration: legacy alarm configuration is read-only.
BEGIN;

-- Make every migration target provably belong to both its migration source
-- and a definition origin carrying that same legacy source identity.
DROP TRIGGER IF EXISTS trg_alarm_definition_origins_append_only
    ON t_alarm_definition_origins;
DROP TRIGGER IF EXISTS trg_legacy_alarm_migration_targets_append_only
    ON t_legacy_alarm_migration_targets;
ALTER TABLE t_legacy_alarm_migration_targets
    DROP CONSTRAINT IF EXISTS fk_legacy_alarm_target_migration_source,
    DROP CONSTRAINT IF EXISTS fk_legacy_alarm_target_definition_origin;

ALTER TABLE t_alarm_definition_origins
    ADD COLUMN IF NOT EXISTS source_kind TEXT,
    ADD COLUMN IF NOT EXISTS source_key TEXT;
UPDATE t_alarm_definition_origins
SET source_kind = details->>'source_kind',
    source_key = details->>'source_key'
WHERE origin_type = 'legacy_migration';
ALTER TABLE t_alarm_definition_origins
    DROP CONSTRAINT IF EXISTS chk_alarm_definition_origin_legacy_source,
    DROP CONSTRAINT IF EXISTS uq_alarm_definition_origin_legacy_source,
    ADD CONSTRAINT chk_alarm_definition_origin_legacy_source CHECK (
        (
            origin_type = 'legacy_migration'
            AND source_kind IS NOT NULL
            AND source_key IS NOT NULL
            AND length(btrim(source_kind)) > 0
            AND length(btrim(source_key)) > 0
        )
        OR
        (
            origin_type <> 'legacy_migration'
            AND source_kind IS NULL
            AND source_key IS NULL
        )
    ),
    ADD CONSTRAINT uq_alarm_definition_origin_legacy_source
        UNIQUE (definition_id, source_kind, source_key, origin_type);

ALTER TABLE t_legacy_alarm_migrations
    DROP CONSTRAINT IF EXISTS uq_legacy_alarm_migration_identity,
    ADD CONSTRAINT uq_legacy_alarm_migration_identity
        UNIQUE (id, source_kind, source_key);

ALTER TABLE t_legacy_alarm_migration_targets
    ADD COLUMN IF NOT EXISTS source_kind TEXT,
    ADD COLUMN IF NOT EXISTS source_key TEXT,
    ADD COLUMN IF NOT EXISTS origin_type TEXT;
UPDATE t_legacy_alarm_migration_targets target
SET source_kind = migration.source_kind,
    source_key = migration.source_key,
    origin_type = 'legacy_migration'
FROM t_legacy_alarm_migrations migration
WHERE migration.id = target.migration_id;
ALTER TABLE t_legacy_alarm_migration_targets
    ALTER COLUMN source_kind SET NOT NULL,
    ALTER COLUMN source_key SET NOT NULL,
    ALTER COLUMN origin_type SET DEFAULT 'legacy_migration',
    ALTER COLUMN origin_type SET NOT NULL,
    DROP CONSTRAINT IF EXISTS chk_legacy_alarm_target_origin_type,
    DROP CONSTRAINT IF EXISTS fk_legacy_alarm_target_migration_source,
    DROP CONSTRAINT IF EXISTS fk_legacy_alarm_target_definition_origin,
    ADD CONSTRAINT chk_legacy_alarm_target_origin_type
        CHECK (origin_type = 'legacy_migration'),
    ADD CONSTRAINT fk_legacy_alarm_target_migration_source
        FOREIGN KEY (migration_id, source_kind, source_key)
        REFERENCES t_legacy_alarm_migrations(id, source_kind, source_key)
        DEFERRABLE INITIALLY DEFERRED,
    ADD CONSTRAINT fk_legacy_alarm_target_definition_origin
        FOREIGN KEY (definition_id, source_kind, source_key, origin_type)
        REFERENCES t_alarm_definition_origins(
            definition_id, source_kind, source_key, origin_type
        )
        DEFERRABLE INITIALLY DEFERRED;

CREATE TRIGGER trg_alarm_definition_origins_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_alarm_definition_origins
    FOR EACH STATEMENT EXECUTE FUNCTION reject_alarm_definition_origin_mutation();
CREATE TRIGGER trg_legacy_alarm_migration_targets_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_legacy_alarm_migration_targets
    FOR EACH STATEMENT EXECUTE FUNCTION reject_legacy_alarm_migration_mutation();

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

CREATE OR REPLACE FUNCTION reject_legacy_alarm_tag_delete()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.alarm_level IS NOT NULL
       OR OLD.alarm_type IS NOT NULL
       OR OLD.alarm_threshold IS NOT NULL
       OR OLD.fault_map_id IS NOT NULL THEN
        RAISE EXCEPTION 'legacy alarm tag cannot be deleted before migration';
    END IF;
    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_tags_reject_legacy_alarm_delete ON t_tags;
CREATE TRIGGER trg_tags_reject_legacy_alarm_delete
    BEFORE DELETE ON t_tags
    FOR EACH ROW EXECUTE FUNCTION reject_legacy_alarm_tag_delete();

COMMIT;
