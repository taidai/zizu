-- Point-processing hard cut and EN9 BOOLEAN_SET foundation.
-- This migration is deliberately replay-safe for restore rehearsals, but it
-- refuses mixed old/new table identities.
BEGIN;

DO $$
DECLARE
  has_old BOOLEAN := to_regclass('public.t_point_conversion_templates') IS NOT NULL;
  has_new BOOLEAN := to_regclass('public.t_point_processing_templates') IS NOT NULL;
BEGIN
  IF has_old AND has_new THEN
    RAISE EXCEPTION 'schema 040 refuses mixed point conversion/processing tables';
  END IF;
  IF NOT has_old AND NOT has_new THEN
    RAISE EXCEPTION 'schema 040 requires complete schema 039';
  END IF;
  IF NOT has_old THEN
    RETURN;
  END IF;

  ALTER TABLE t_point_conversion_templates RENAME TO t_point_processing_templates;
  ALTER TABLE t_point_conversion_revisions RENAME TO t_point_processing_revisions;
  ALTER TABLE t_solution_point_conversion_assets RENAME TO t_solution_point_processing_assets;
  ALTER TABLE t_point_conversion_inputs RENAME TO t_point_processing_inputs;
  ALTER TABLE t_point_conversion_outputs RENAME TO t_point_processing_outputs;
  ALTER TABLE t_point_conversion_plans RENAME TO t_point_processing_plans;
  ALTER TABLE t_point_conversion_plan_items RENAME TO t_point_processing_plan_items;
  ALTER TABLE t_installed_point_conversions RENAME TO t_installed_point_processings;
  ALTER TABLE t_conversion_input_bindings RENAME TO t_point_processing_input_bindings;
  ALTER TABLE t_conversion_output_bindings RENAME TO t_point_processing_output_bindings;
  ALTER TABLE t_point_conversion_applications RENAME TO t_point_processing_applications;
  ALTER TABLE t_point_conversion_idempotency RENAME TO t_point_processing_idempotency;

  ALTER TABLE t_solution_install_plans
    RENAME COLUMN point_conversion_plans TO point_processing_plans;
  ALTER TABLE t_point_processing_input_bindings
    RENAME COLUMN installed_conversion_id TO installed_processing_id;
  ALTER TABLE t_point_processing_output_bindings
    RENAME COLUMN installed_conversion_id TO installed_processing_id;
  ALTER TABLE t_point_processing_applications
    RENAME COLUMN installed_conversion_id TO installed_processing_id;
  ALTER TABLE t_l2_observations
    RENAME COLUMN conversion_revision_id TO processing_revision_id;
  ALTER TABLE t_l2_latest
    RENAME COLUMN conversion_revision_id TO processing_revision_id;
END;
$$;

DROP TRIGGER IF EXISTS trg_point_conversion_plans_lifecycle
  ON t_point_processing_plans;
DROP TRIGGER IF EXISTS trg_point_conversion_plans_no_truncate
  ON t_point_processing_plans;
DROP TRIGGER IF EXISTS trg_point_processing_plans_lifecycle
  ON t_point_processing_plans;
DROP TRIGGER IF EXISTS trg_point_processing_plans_no_truncate
  ON t_point_processing_plans;
DROP TRIGGER IF EXISTS trg_t_point_conversion_plan_items_append_only
  ON t_point_processing_plan_items;
DROP TRIGGER IF EXISTS trg_t_point_processing_plan_items_append_only
  ON t_point_processing_plan_items;

ALTER TABLE t_tags
  ADD COLUMN IF NOT EXISTS wire_data_type TEXT,
  ADD COLUMN IF NOT EXISTS value_data_type TEXT,
  ADD COLUMN IF NOT EXISTS source_address TEXT,
  ADD COLUMN IF NOT EXISTS decimal DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS read_only BOOLEAN;

UPDATE t_tags
SET value_data_type = COALESCE(value_data_type, data_type),
    source_address = COALESCE(source_address, source_path),
    read_only = COALESCE(read_only, read_write = 'R')
WHERE value_data_type IS NULL
   OR source_address IS NULL
   OR read_only IS NULL;

ALTER TABLE t_tags
  DROP CONSTRAINT IF EXISTS chk_tags_value_data_type,
  ADD CONSTRAINT chk_tags_value_data_type CHECK (
    value_data_type IS NULL
    OR value_data_type IN ('FLOAT','INT','BOOL','STRING','ENUM','CODE_SET')
  ),
  DROP CONSTRAINT IF EXISTS chk_tags_decimal_finite,
  ADD CONSTRAINT chk_tags_decimal_finite CHECK (
    decimal IS NULL OR (decimal = decimal AND abs(decimal) < 1e308)
  );

ALTER TABLE t_entity_instances
  DROP CONSTRAINT IF EXISTS chk_entity_instance_source_kind;
UPDATE t_entity_instances
SET source_kind = 'point_processing'
WHERE source_kind = 'point_conversion';
ALTER TABLE t_entity_instances
  ADD CONSTRAINT chk_entity_instance_source_kind
    CHECK (source_kind IN ('legacy_tag','point_processing'));

ALTER TABLE t_point_processing_plans
  DROP CONSTRAINT IF EXISTS t_point_conversion_plans_kind_check,
  DROP CONSTRAINT IF EXISTS t_point_processing_plans_kind_check;
ALTER TABLE t_point_processing_plans
  ALTER COLUMN kind SET DEFAULT 'point_processing';
UPDATE t_point_processing_plans
SET kind = 'point_processing'
WHERE kind = 'point_conversion';
ALTER TABLE t_point_processing_plans
  ADD CONSTRAINT t_point_processing_plans_kind_check
    CHECK (kind = 'point_processing');

ALTER TABLE t_point_processing_plan_items
  ADD COLUMN IF NOT EXISTS layer TEXT;
UPDATE t_point_processing_plan_items
SET layer = CASE
  WHEN input_id IS NOT NULL THEN 'L1'
  WHEN output_id IS NOT NULL THEN 'L2'
  ELSE 'L1'
END
WHERE layer IS NULL;
ALTER TABLE t_point_processing_plan_items
  ALTER COLUMN layer SET NOT NULL,
  DROP CONSTRAINT IF EXISTS chk_point_processing_plan_item_layer,
  ADD CONSTRAINT chk_point_processing_plan_item_layer
    CHECK (layer IN ('L0','L1','L2'));

ALTER TABLE t_fault_code_mapping_entries
  DROP COLUMN IF EXISTS default_severity;

CREATE TABLE IF NOT EXISTS t_boolean_set_transform_rules (
  output_id UUID PRIMARY KEY REFERENCES t_point_processing_outputs(id)
);

CREATE TABLE IF NOT EXISTS t_boolean_set_mapping_entries (
  output_id UUID NOT NULL REFERENCES t_point_processing_outputs(id),
  input_id UUID NOT NULL REFERENCES t_point_processing_inputs(id),
  canonical_code TEXT NOT NULL CHECK (btrim(canonical_code) <> ''),
  display_name TEXT NOT NULL CHECK (btrim(display_name) <> ''),
  fault_category TEXT NOT NULL CHECK (btrim(fault_category) <> ''),
  PRIMARY KEY(output_id, input_id),
  UNIQUE(output_id, canonical_code)
);

DROP TRIGGER IF EXISTS trg_conversion_output_binding_single_source
  ON t_point_processing_output_bindings;
DROP TRIGGER IF EXISTS trg_processing_output_binding_single_source
  ON t_point_processing_output_bindings;
DROP TRIGGER IF EXISTS trg_installed_conversion_single_source
  ON t_installed_point_processings;
DROP TRIGGER IF EXISTS trg_installed_processing_single_source
  ON t_installed_point_processings;

CREATE OR REPLACE FUNCTION assert_entity_instance_single_source(target_id UUID)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  kind TEXT;
  active_tags INTEGER;
  active_outputs INTEGER;
BEGIN
  SELECT source_kind INTO kind
  FROM t_entity_instances
  WHERE id = target_id;

  IF kind IS NULL THEN
    RETURN;
  END IF;

  SELECT count(*) INTO active_tags
  FROM t_entity_instance_bindings
  WHERE entity_instance_id = target_id
    AND active = TRUE;

  SELECT count(*) INTO active_outputs
  FROM t_point_processing_output_bindings AS binding
  JOIN t_installed_point_processings AS installed
    ON installed.id = binding.installed_processing_id
  WHERE binding.entity_instance_id = target_id
    AND installed.current = TRUE;

  IF kind = 'point_processing'
     AND (active_tags <> 0 OR active_outputs <> 1) THEN
    RAISE EXCEPTION
      'point processing entity must have exactly one processing source and no direct tag source'
      USING ERRCODE = '23514';
  END IF;

  IF kind = 'legacy_tag' AND active_outputs <> 0 THEN
    RAISE EXCEPTION
      'legacy entity cannot have a point processing source'
      USING ERRCODE = '23514';
  END IF;
END;
$$;

DROP FUNCTION IF EXISTS enforce_installed_conversion_sources() CASCADE;
CREATE OR REPLACE FUNCTION enforce_installed_processing_sources()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  target_id UUID;
  installed_id UUID;
BEGIN
  installed_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
  FOR target_id IN
    SELECT DISTINCT entity_instance_id
    FROM t_point_processing_output_bindings
    WHERE installed_processing_id = installed_id
  LOOP
    PERFORM assert_entity_instance_single_source(target_id);
  END LOOP;
  RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_installed_processing_single_source
AFTER INSERT OR UPDATE OF current OR DELETE ON t_installed_point_processings
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION enforce_installed_processing_sources();

CREATE CONSTRAINT TRIGGER trg_processing_output_binding_single_source
AFTER INSERT OR UPDATE OR DELETE ON t_point_processing_output_bindings
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION enforce_entity_instance_binding_source();

DROP FUNCTION IF EXISTS enforce_point_conversion_plan_append_only();

CREATE OR REPLACE FUNCTION enforce_point_processing_plan_append_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE'
     AND OLD.status IN ('ready','blocked')
     AND NEW.status = 'applied'
     AND (to_jsonb(NEW) - 'status') = (to_jsonb(OLD) - 'status') THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'point processing plans are append-only'
    USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER trg_point_processing_plans_lifecycle
BEFORE UPDATE OR DELETE ON t_point_processing_plans
FOR EACH ROW EXECUTE FUNCTION enforce_point_processing_plan_append_only();

CREATE TRIGGER trg_point_processing_plans_no_truncate
BEFORE TRUNCATE ON t_point_processing_plans
FOR EACH STATEMENT EXECUTE FUNCTION reject_data_trunk_append_only();

DO $$
DECLARE
  item RECORD;
  new_name TEXT;
BEGIN
  FOR item IN
    SELECT c.oid, c.relname
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind IN ('i','I')
      AND (
        c.relname LIKE '%point_conversion%'
        OR c.relname LIKE '%installed_conversion%'
        OR c.relname LIKE '%conversion_input%'
        OR c.relname LIKE '%conversion_output%'
      )
  LOOP
    new_name := replace(item.relname, 'point_conversion', 'point_processing');
    new_name := replace(new_name, 'installed_conversion', 'installed_processing');
    new_name := replace(new_name, 'conversion_input', 'point_processing_input');
    new_name := replace(new_name, 'conversion_output', 'point_processing_output');
    IF new_name <> item.relname AND to_regclass('public.' || new_name) IS NULL THEN
      EXECUTE format('ALTER INDEX %I RENAME TO %I', item.relname, new_name);
    END IF;
  END LOOP;
END;
$$;

DO $$
DECLARE
  table_name TEXT;
  trigger_name TEXT;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    't_point_processing_plan_items',
    't_boolean_set_transform_rules',
    't_boolean_set_mapping_entries'
  ] LOOP
    trigger_name := 'trg_' || table_name || '_append_only';
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', trigger_name, table_name);
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE OR TRUNCATE ON %I '
      'FOR EACH STATEMENT EXECUTE FUNCTION reject_data_trunk_append_only()',
      trigger_name,
      table_name
    );
  END LOOP;
END;
$$;

COMMIT;
