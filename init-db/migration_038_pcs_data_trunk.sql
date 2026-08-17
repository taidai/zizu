-- PCS L0-L1-L2 data trunk (expand phase).
-- Runtime consumers remain on their existing paths until migration 039.
BEGIN;

ALTER TABLE t_telemetry
  ADD COLUMN IF NOT EXISTS observation_id UUID,
  ADD COLUMN IF NOT EXISTS source_message_id TEXT,
  ADD COLUMN IF NOT EXISTS source_sequence BIGINT,
  ADD COLUMN IF NOT EXISTS source_digest CHAR(64),
  ADD COLUMN IF NOT EXISTS raw_unit TEXT,
  ADD COLUMN IF NOT EXISTS raw_value_float DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS raw_value_int BIGINT,
  ADD COLUMN IF NOT EXISTS raw_value_bool BOOLEAN,
  ADD COLUMN IF NOT EXISTS raw_value_text TEXT;

ALTER TABLE t_telemetry_latest
  ADD COLUMN IF NOT EXISTS observation_id UUID,
  ADD COLUMN IF NOT EXISTS source_message_id TEXT,
  ADD COLUMN IF NOT EXISTS source_sequence BIGINT,
  ADD COLUMN IF NOT EXISTS source_digest CHAR(64),
  ADD COLUMN IF NOT EXISTS source_order_key TEXT,
  ADD COLUMN IF NOT EXISTS raw_unit TEXT,
  ADD COLUMN IF NOT EXISTS raw_value_float DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS raw_value_int BIGINT,
  ADD COLUMN IF NOT EXISTS raw_value_bool BOOLEAN,
  ADD COLUMN IF NOT EXISTS raw_value_text TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_telemetry_source_observation
ON t_telemetry(tag_id, ts, source_digest)
WHERE source_digest IS NOT NULL;

CREATE TABLE IF NOT EXISTS t_l0_observation_dedup (
  observation_id UUID PRIMARY KEY,
  tag_id UUID NOT NULL REFERENCES t_tags(id),
  observed_at TIMESTAMPTZ NOT NULL,
  source_digest CHAR(64) NOT NULL UNIQUE,
  source_message_id TEXT,
  source_sequence BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE t_telemetry
  DROP CONSTRAINT IF EXISTS fk_telemetry_l0_observation,
  ADD CONSTRAINT fk_telemetry_l0_observation
    FOREIGN KEY(observation_id)
    REFERENCES t_l0_observation_dedup(observation_id),
  DROP CONSTRAINT IF EXISTS chk_telemetry_raw_typed_value,
  ADD CONSTRAINT chk_telemetry_raw_typed_value CHECK (
    source_digest IS NULL
    OR (
      observation_id IS NOT NULL
      AND num_nonnulls(
        raw_value_float,
        raw_value_int,
        raw_value_bool,
        raw_value_text
      ) = 1
    )
  );

ALTER TABLE t_telemetry_latest
  DROP CONSTRAINT IF EXISTS fk_telemetry_latest_l0_observation,
  ADD CONSTRAINT fk_telemetry_latest_l0_observation
    FOREIGN KEY(observation_id)
    REFERENCES t_l0_observation_dedup(observation_id),
  DROP CONSTRAINT IF EXISTS chk_telemetry_latest_raw_typed_value,
  ADD CONSTRAINT chk_telemetry_latest_raw_typed_value CHECK (
    source_digest IS NULL
    OR (
      observation_id IS NOT NULL
      AND num_nonnulls(
        raw_value_float,
        raw_value_int,
        raw_value_bool,
        raw_value_text
      ) = 1
    )
  );

ALTER TABLE t_entity_instances
  ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'legacy_tag',
  DROP CONSTRAINT IF EXISTS t_entity_instances_data_type_check;

ALTER TABLE t_entity_instances
  ADD CONSTRAINT t_entity_instances_data_type_check
    CHECK (data_type IN ('FLOAT','INT','BOOL','STRING','ENUM','CODE_SET')),
  DROP CONSTRAINT IF EXISTS chk_entity_instance_source_kind,
  ADD CONSTRAINT chk_entity_instance_source_kind
    CHECK (source_kind IN ('legacy_tag','point_conversion'));

ALTER TABLE t_device_instances
  ADD COLUMN IF NOT EXISTS node_id UUID REFERENCES t_nodes(id);

ALTER TABLE t_solution_install_plans
  ADD COLUMN IF NOT EXISTS point_conversion_plans JSONB
    NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS t_point_conversion_templates (
  id UUID PRIMARY KEY,
  asset_id TEXT NOT NULL,
  device_category TEXT NOT NULL,
  brand TEXT NOT NULL,
  model TEXT NOT NULL,
  display_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','retired')),
  UNIQUE(asset_id, brand, model)
);

CREATE TABLE IF NOT EXISTS t_point_conversion_revisions (
  id UUID PRIMARY KEY,
  template_id UUID NOT NULL REFERENCES t_point_conversion_templates(id),
  revision INTEGER NOT NULL CHECK (revision > 0),
  content_digest CHAR(64) NOT NULL,
  published_at TIMESTAMPTZ NOT NULL,
  UNIQUE(template_id, revision),
  UNIQUE(template_id, content_digest)
);

CREATE TABLE IF NOT EXISTS t_solution_point_conversion_assets (
  package_record_id UUID NOT NULL REFERENCES t_solution_packages(id),
  template_revision_id UUID NOT NULL REFERENCES t_point_conversion_revisions(id),
  asset_id TEXT NOT NULL,
  PRIMARY KEY(package_record_id, asset_id),
  UNIQUE(package_record_id, template_revision_id)
);

CREATE TABLE IF NOT EXISTS t_point_conversion_inputs (
  id UUID PRIMARY KEY,
  revision_id UUID NOT NULL REFERENCES t_point_conversion_revisions(id),
  input_key TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('l0','l2')),
  data_type TEXT NOT NULL
    CHECK (data_type IN ('FLOAT','INT','BOOL','STRING','ENUM','CODE_SET')),
  unit TEXT,
  required BOOLEAN NOT NULL,
  stable_source_key TEXT NOT NULL,
  aliases TEXT[] NOT NULL DEFAULT '{}',
  UNIQUE(revision_id, input_key)
);

CREATE TABLE IF NOT EXISTS t_point_conversion_outputs (
  id UUID PRIMARY KEY,
  revision_id UUID NOT NULL REFERENCES t_point_conversion_revisions(id),
  output_key TEXT NOT NULL,
  entity_definition_id TEXT NOT NULL,
  data_type TEXT NOT NULL
    CHECK (data_type IN ('FLOAT','INT','BOOL','STRING','ENUM','CODE_SET')),
  unit TEXT,
  freshness_seconds DOUBLE PRECISION NOT NULL
    CHECK (
      freshness_seconds > 0
      AND freshness_seconds = freshness_seconds
      AND abs(freshness_seconds) < 1e308
    ),
  UNIQUE(revision_id, output_key)
);

CREATE TABLE IF NOT EXISTS t_numeric_transform_rules (
  output_id UUID PRIMARY KEY REFERENCES t_point_conversion_outputs(id),
  input_id UUID NOT NULL REFERENCES t_point_conversion_inputs(id),
  scale DOUBLE PRECISION NOT NULL
    CHECK (scale = scale AND abs(scale) < 1e308),
  "offset" DOUBLE PRECISION NOT NULL
    CHECK ("offset" = "offset" AND abs("offset") < 1e308),
  minimum DOUBLE PRECISION
    CHECK (minimum = minimum AND abs(minimum) < 1e308),
  maximum DOUBLE PRECISION
    CHECK (maximum = maximum AND abs(maximum) < 1e308),
  CHECK (minimum IS NULL OR maximum IS NULL OR minimum <= maximum)
);

CREATE TABLE IF NOT EXISTS t_enum_transform_rules (
  output_id UUID PRIMARY KEY REFERENCES t_point_conversion_outputs(id),
  input_id UUID NOT NULL REFERENCES t_point_conversion_inputs(id)
);

CREATE TABLE IF NOT EXISTS t_fault_code_transform_rules (
  output_id UUID PRIMARY KEY REFERENCES t_point_conversion_outputs(id),
  input_id UUID NOT NULL REFERENCES t_point_conversion_inputs(id),
  delimiter TEXT NOT NULL
    CHECK (delimiter IN ('semicolon','comma','pipe','whitespace'))
);

CREATE TABLE IF NOT EXISTS t_enum_mapping_entries (
  output_id UUID NOT NULL REFERENCES t_point_conversion_outputs(id),
  raw_value TEXT NOT NULL,
  canonical_value TEXT NOT NULL,
  PRIMARY KEY(output_id, raw_value)
);

CREATE TABLE IF NOT EXISTS t_fault_code_mapping_entries (
  output_id UUID NOT NULL REFERENCES t_point_conversion_outputs(id),
  raw_code TEXT NOT NULL,
  canonical_code TEXT NOT NULL,
  display_name TEXT NOT NULL,
  default_severity TEXT NOT NULL
    CHECK (default_severity IN ('CRITICAL','MAJOR','WARNING','INFO')),
  PRIMARY KEY(output_id, raw_code)
);

CREATE TABLE IF NOT EXISTS t_point_conversion_plans (
  id UUID PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'point_conversion'
    CHECK (kind = 'point_conversion'),
  node_id UUID NOT NULL REFERENCES t_nodes(id),
  template_revision_id UUID NOT NULL REFERENCES t_point_conversion_revisions(id),
  entity_identity_installation_id UUID NOT NULL,
  solution_installation_id UUID NOT NULL,
  base_site_configuration_version BIGINT NOT NULL,
  source_catalog_digest CHAR(64) NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ready','blocked','applied')),
  items JSONB NOT NULL,
  blockers JSONB NOT NULL,
  digest CHAR(64) NOT NULL,
  planned_by TEXT NOT NULL CHECK (btrim(planned_by) <> ''),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t_point_conversion_plan_items (
  plan_id UUID NOT NULL REFERENCES t_point_conversion_plans(id),
  item_key TEXT NOT NULL,
  action TEXT NOT NULL
    CHECK (action IN ('add','update','preserve','delete_candidate','block')),
  input_id UUID REFERENCES t_point_conversion_inputs(id),
  output_id UUID REFERENCES t_point_conversion_outputs(id),
  source_kind TEXT CHECK (source_kind IN ('l0','l2')),
  selected_tag_id UUID REFERENCES t_tags(id),
  selected_entity_instance_id UUID REFERENCES t_entity_instances(id),
  output_entity_instance_id UUID,
  blocker_code TEXT,
  before_value JSONB,
  after_value JSONB,
  PRIMARY KEY(plan_id, item_key),
  CHECK (
    source_kind IS NULL
    OR (
      source_kind = 'l0'
      AND selected_tag_id IS NOT NULL
      AND selected_entity_instance_id IS NULL
    )
    OR (
      source_kind = 'l2'
      AND selected_tag_id IS NULL
      AND selected_entity_instance_id IS NOT NULL
    )
  )
);

CREATE TABLE IF NOT EXISTS t_installed_point_conversions (
  id UUID PRIMARY KEY,
  node_id UUID NOT NULL REFERENCES t_nodes(id),
  revision_id UUID NOT NULL REFERENCES t_point_conversion_revisions(id),
  source_plan_id UUID NOT NULL REFERENCES t_point_conversion_plans(id),
  solution_installation_id UUID NOT NULL
    REFERENCES t_solution_installations(id) DEFERRABLE INITIALLY DEFERRED,
  site_configuration_version BIGINT NOT NULL,
  installed_by TEXT NOT NULL CHECK (btrim(installed_by) <> ''),
  installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  current BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_installed_point_conversion_current
ON t_installed_point_conversions(node_id)
WHERE current = TRUE;

CREATE TABLE IF NOT EXISTS t_conversion_input_bindings (
  installed_conversion_id UUID NOT NULL
    REFERENCES t_installed_point_conversions(id),
  input_id UUID NOT NULL REFERENCES t_point_conversion_inputs(id),
  source_kind TEXT NOT NULL CHECK (source_kind IN ('l0','l2')),
  l0_tag_id UUID REFERENCES t_tags(id),
  l2_entity_instance_id UUID REFERENCES t_entity_instances(id),
  confirmed_by TEXT NOT NULL CHECK (btrim(confirmed_by) <> ''),
  confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(installed_conversion_id, input_id),
  CHECK (
    (
      source_kind = 'l0'
      AND l0_tag_id IS NOT NULL
      AND l2_entity_instance_id IS NULL
    )
    OR (
      source_kind = 'l2'
      AND l0_tag_id IS NULL
      AND l2_entity_instance_id IS NOT NULL
    )
  )
);

CREATE TABLE IF NOT EXISTS t_conversion_output_bindings (
  installed_conversion_id UUID NOT NULL
    REFERENCES t_installed_point_conversions(id),
  output_id UUID NOT NULL REFERENCES t_point_conversion_outputs(id),
  entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  PRIMARY KEY(installed_conversion_id, output_id),
  UNIQUE(installed_conversion_id, entity_instance_id)
);

CREATE TABLE IF NOT EXISTS t_point_conversion_applications (
  id UUID PRIMARY KEY,
  plan_id UUID NOT NULL REFERENCES t_point_conversion_plans(id),
  installed_conversion_id UUID NOT NULL
    REFERENCES t_installed_point_conversions(id),
  solution_installation_id UUID NOT NULL
    REFERENCES t_solution_installations(id) DEFERRABLE INITIALLY DEFERRED,
  site_configuration_version BIGINT NOT NULL,
  actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
  output_entity_instance_ids UUID[] NOT NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t_point_conversion_idempotency (
  actor TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_digest CHAR(64) NOT NULL,
  application_id UUID NOT NULL REFERENCES t_point_conversion_applications(id),
  PRIMARY KEY(actor, idempotency_key)
);

CREATE TABLE IF NOT EXISTS t_l2_observations (
  observed_at TIMESTAMPTZ NOT NULL,
  event_id UUID NOT NULL,
  entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  received_at TIMESTAMPTZ NOT NULL,
  calculated_at TIMESTAMPTZ NOT NULL,
  value_float DOUBLE PRECISION,
  value_int BIGINT,
  value_bool BOOLEAN,
  value_text TEXT,
  value_codes TEXT[],
  quality SMALLINT NOT NULL CHECK (quality IN (0,1,64,192)),
  reason TEXT,
  conversion_revision_id UUID NOT NULL
    REFERENCES t_point_conversion_revisions(id),
  site_configuration_version BIGINT NOT NULL,
  source_digest CHAR(64) NOT NULL,
  source_order_key TEXT NOT NULL,
  CONSTRAINT uq_l2_event_observed_at UNIQUE(event_id, observed_at),
  CONSTRAINT chk_l2_typed_value CHECK (
    (
      quality IN (0,1)
      AND num_nonnulls(
        value_float,
        value_int,
        value_bool,
        value_text,
        value_codes
      ) = 0
    )
    OR (
      quality IN (64,192)
      AND num_nonnulls(
        value_float,
        value_int,
        value_bool,
        value_text,
        value_codes
      ) = 1
    )
  )
);

SELECT create_hypertable(
  't_l2_observations',
  'observed_at',
  if_not_exists => TRUE
);

CREATE TABLE IF NOT EXISTS t_l2_latest (
  entity_instance_id UUID PRIMARY KEY REFERENCES t_entity_instances(id),
  event_id UUID NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL,
  calculated_at TIMESTAMPTZ NOT NULL,
  value_float DOUBLE PRECISION,
  value_int BIGINT,
  value_bool BOOLEAN,
  value_text TEXT,
  value_codes TEXT[],
  quality SMALLINT NOT NULL CHECK (quality IN (0,1,64,192)),
  reason TEXT,
  conversion_revision_id UUID NOT NULL
    REFERENCES t_point_conversion_revisions(id),
  site_configuration_version BIGINT NOT NULL,
  source_digest CHAR(64) NOT NULL,
  source_order_key TEXT NOT NULL,
  CONSTRAINT chk_l2_latest_typed_value CHECK (
    (
      quality IN (0,1)
      AND num_nonnulls(
        value_float,
        value_int,
        value_bool,
        value_text,
        value_codes
      ) = 0
    )
    OR (
      quality IN (64,192)
      AND num_nonnulls(
        value_float,
        value_int,
        value_bool,
        value_text,
        value_codes
      ) = 1
    )
  )
);

CREATE TABLE IF NOT EXISTS t_l2_observation_sources (
  l2_event_id UUID NOT NULL,
  l2_observed_at TIMESTAMPTZ NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('l0','l2','freshness')),
  l0_observation_id UUID REFERENCES t_l0_observation_dedup(observation_id),
  source_l2_event_id UUID,
  source_l2_observed_at TIMESTAMPTZ,
  source_digest CHAR(64) NOT NULL,
  PRIMARY KEY(l2_event_id, l2_observed_at, source_kind, source_digest),
  FOREIGN KEY(l2_event_id, l2_observed_at)
    REFERENCES t_l2_observations(event_id, observed_at),
  FOREIGN KEY(source_l2_event_id, source_l2_observed_at)
    REFERENCES t_l2_observations(event_id, observed_at),
  CHECK (
    (
      source_kind = 'l0'
      AND l0_observation_id IS NOT NULL
      AND source_l2_event_id IS NULL
      AND source_l2_observed_at IS NULL
    )
    OR (
      source_kind IN ('l2','freshness')
      AND l0_observation_id IS NULL
      AND source_l2_event_id IS NOT NULL
      AND source_l2_observed_at IS NOT NULL
    )
  )
);

CREATE TABLE IF NOT EXISTS t_l2_stream_outbox (
  event_id UUID PRIMARY KEY,
  entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  claimed_by UUID,
  claimed_until TIMESTAMPTZ,
  CHECK ((claimed_by IS NULL) = (claimed_until IS NULL))
);

CREATE TABLE IF NOT EXISTS t_ingestion_failures (
  id UUID PRIMARY KEY,
  source_digest CHAR(64) NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('parse','l0','conversion','l2','outbox')),
  safe_summary JSONB NOT NULL,
  attempts INTEGER NOT NULL CHECK (attempts > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);

CREATE OR REPLACE FUNCTION validate_l2_typed_value_against_entity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  expected_type TEXT;
  value_count INTEGER;
  value_matches BOOLEAN;
BEGIN
  SELECT data_type
  INTO expected_type
  FROM t_entity_instances
  WHERE id = NEW.entity_instance_id;

  IF expected_type IS NULL THEN
    RETURN NEW;
  END IF;

  value_count := num_nonnulls(
    NEW.value_float,
    NEW.value_int,
    NEW.value_bool,
    NEW.value_text,
    NEW.value_codes
  );

  IF NEW.quality IN (0,1) THEN
    value_matches := value_count = 0;
  ELSE
    value_matches := value_count = 1 AND CASE expected_type
      WHEN 'FLOAT' THEN NEW.value_float IS NOT NULL
      WHEN 'INT' THEN NEW.value_int IS NOT NULL
      WHEN 'BOOL' THEN NEW.value_bool IS NOT NULL
      WHEN 'STRING' THEN NEW.value_text IS NOT NULL
      WHEN 'ENUM' THEN NEW.value_text IS NOT NULL
      WHEN 'CODE_SET' THEN NEW.value_codes IS NOT NULL
      ELSE FALSE
    END;
  END IF;

  IF NOT value_matches THEN
    RAISE EXCEPTION 'L2 typed value does not match entity data_type %', expected_type
      USING ERRCODE = '23514', CONSTRAINT = 'chk_l2_entity_data_type';
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_l2_observation_value
  ON t_l2_observations;
CREATE TRIGGER trg_validate_l2_observation_value
BEFORE INSERT OR UPDATE ON t_l2_observations
FOR EACH ROW EXECUTE FUNCTION validate_l2_typed_value_against_entity();

DROP TRIGGER IF EXISTS trg_validate_l2_latest_value ON t_l2_latest;
CREATE TRIGGER trg_validate_l2_latest_value
BEFORE INSERT OR UPDATE ON t_l2_latest
FOR EACH ROW EXECUTE FUNCTION validate_l2_typed_value_against_entity();

CREATE OR REPLACE FUNCTION reject_data_trunk_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
    USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION enforce_point_conversion_plan_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'UPDATE'
     AND OLD.status IN ('ready','blocked')
     AND NEW.status = 'applied'
     AND (to_jsonb(NEW) - 'status') = (to_jsonb(OLD) - 'status') THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'point conversion plans are append-only'
    USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS trg_point_conversion_plans_lifecycle
  ON t_point_conversion_plans;
CREATE TRIGGER trg_point_conversion_plans_lifecycle
BEFORE UPDATE OR DELETE ON t_point_conversion_plans
FOR EACH ROW EXECUTE FUNCTION enforce_point_conversion_plan_append_only();

DROP TRIGGER IF EXISTS trg_point_conversion_plans_no_truncate
  ON t_point_conversion_plans;
CREATE TRIGGER trg_point_conversion_plans_no_truncate
BEFORE TRUNCATE ON t_point_conversion_plans
FOR EACH STATEMENT EXECUTE FUNCTION reject_data_trunk_append_only();

DO $$
DECLARE
  table_name TEXT;
  trigger_name TEXT;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    't_l0_observation_dedup',
    't_point_conversion_revisions',
    't_solution_point_conversion_assets',
    't_point_conversion_inputs',
    't_point_conversion_outputs',
    't_numeric_transform_rules',
    't_enum_transform_rules',
    't_fault_code_transform_rules',
    't_enum_mapping_entries',
    't_fault_code_mapping_entries',
    't_point_conversion_plan_items',
    't_point_conversion_applications',
    't_point_conversion_idempotency',
    't_l2_observations',
    't_l2_observation_sources'
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
