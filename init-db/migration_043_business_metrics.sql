-- Schema 043: immutable business-metric delivery records and recoverable projections.
-- This migration is expand-only over the complete Schema 042 data trunk.

DO $$
DECLARE
  existing_tables INTEGER;
  existing_contract_columns INTEGER;
  schema_042_tables INTEGER;
BEGIN
  SELECT count(*) INTO existing_tables
  FROM (VALUES
    ('t_business_metric_templates'),
    ('t_business_metric_revisions'),
    ('t_business_metric_installation_plans'),
    ('t_business_metric_plan_items'),
    ('t_installed_business_metrics'),
    ('t_business_metric_source_bindings'),
    ('t_business_metric_projections'),
    ('t_business_metric_window_results'),
    ('t_business_metric_recomputations'),
    ('t_entity_capability_contracts'),
    ('t_business_metric_audit'),
    ('t_business_metric_acceptance_reports')
  ) AS expected(name)
  WHERE to_regclass('public.' || expected.name) IS NOT NULL;

  IF existing_tables NOT IN (0, 12) THEN
    RAISE EXCEPTION 'schema 043 is partially present' USING ERRCODE = '55000';
  END IF;

  SELECT count(*) INTO schema_042_tables
  FROM (VALUES
    ('t_point_processing_expressions'),
    ('t_point_processing_selectors'),
    ('t_point_processing_selector_members'),
    ('t_point_processing_dependencies'),
    ('t_point_processing_formula_runs'),
    ('t_cross_node_processing_acceptance_reports')
  ) AS required(name)
  WHERE to_regclass('public.' || required.name) IS NOT NULL;

  IF schema_042_tables <> 6
     OR NOT EXISTS (
       SELECT 1 FROM information_schema.columns
       WHERE table_schema = 'public' AND table_name = 't_nodes'
         AND column_name = 'parent_id'
     )
     OR NOT EXISTS (
       SELECT 1 FROM information_schema.columns
       WHERE table_schema = 'public'
         AND table_name = 't_point_processing_expressions'
         AND column_name = 'canonical_ast'
     ) THEN
    RAISE EXCEPTION 'schema 043 requires a complete schema 042' USING ERRCODE = '55000';
  END IF;

  IF existing_tables = 12 THEN
    SELECT count(*) INTO existing_contract_columns
    FROM (VALUES
      ('t_business_metric_templates', 'template_key'),
      ('t_business_metric_revisions', 'content_digest'),
      ('t_business_metric_installation_plans', 'previous_installation_id'),
      ('t_business_metric_plan_items', 'item_kind'),
      ('t_installed_business_metrics', 'installed_processing_id'),
      ('t_business_metric_source_bindings', 'source_digest'),
      ('t_business_metric_projections', 'watermark_at'),
      ('t_business_metric_window_results', 'calculation_method'),
      ('t_business_metric_recomputations', 'request_id'),
      ('t_entity_capability_contracts', 'temporal_semantics'),
      ('t_business_metric_audit', 'resulting_state'),
      ('t_business_metric_acceptance_reports', 'runtime_instance_id')
    ) AS required(table_name, column_name)
    WHERE EXISTS (
      SELECT 1 FROM information_schema.columns AS columns
      WHERE columns.table_schema = 'public'
        AND columns.table_name = required.table_name
        AND columns.column_name = required.column_name
    );
    IF existing_contract_columns <> 12 THEN
      RAISE EXCEPTION 'schema 043 existing structure is malformed'
        USING ERRCODE = '55000';
    END IF;
  END IF;
END;
$$;

-- A physical/logical node has one ordinary current point-processing program,
-- while each installed business metric owns an independent private program.
-- Keeping that distinction on the shared installation record lets Task 2
-- reuse the existing atomic apply path without one metric superseding another.
ALTER TABLE t_installed_point_processings
  ADD COLUMN IF NOT EXISTS processing_scope TEXT NOT NULL DEFAULT 'node';
ALTER TABLE t_installed_point_processings
  ADD COLUMN IF NOT EXISTS processing_owner_key UUID;
ALTER TABLE t_point_processing_revisions
  ADD COLUMN IF NOT EXISTS internal_kind TEXT;
ALTER TABLE t_point_processing_revisions
  DROP CONSTRAINT IF EXISTS chk_point_processing_revision_internal_kind;
ALTER TABLE t_point_processing_revisions
  ADD CONSTRAINT chk_point_processing_revision_internal_kind
  CHECK (internal_kind IS NULL OR internal_kind = 'business_metric');
ALTER TABLE t_installed_point_processings
  DROP CONSTRAINT IF EXISTS chk_installed_point_processing_scope;
ALTER TABLE t_installed_point_processings
  ADD CONSTRAINT chk_installed_point_processing_scope
  CHECK (
    (processing_scope = 'node' AND processing_owner_key IS NULL)
    OR (processing_scope = 'business_metric' AND processing_owner_key IS NOT NULL)
  );

DROP INDEX IF EXISTS uq_installed_point_conversion_current;
DROP INDEX IF EXISTS uq_installed_point_processing_current;
CREATE UNIQUE INDEX uq_installed_point_processing_current
ON t_installed_point_processings(node_id)
WHERE current = TRUE AND processing_scope = 'node';
CREATE UNIQUE INDEX IF NOT EXISTS uq_installed_business_metric_processing_current
ON t_installed_point_processings(node_id, processing_owner_key)
WHERE current = TRUE AND processing_scope = 'business_metric';

CREATE TABLE IF NOT EXISTS t_business_metric_templates (
  id UUID PRIMARY KEY,
  template_key TEXT NOT NULL UNIQUE CHECK (btrim(template_key) <> ''),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t_business_metric_revisions (
  id UUID PRIMARY KEY,
  template_id UUID NOT NULL REFERENCES t_business_metric_templates(id),
  revision INTEGER NOT NULL CHECK (revision > 0),
  content JSONB NOT NULL CHECK (jsonb_typeof(content) = 'object'),
  content_digest CHAR(64) NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
  package_record_id UUID REFERENCES t_solution_packages(id),
  published_at TIMESTAMPTZ NOT NULL,
  UNIQUE(template_id, revision),
  UNIQUE(template_id, content_digest)
);

CREATE TABLE IF NOT EXISTS t_business_metric_installation_plans (
  id UUID PRIMARY KEY,
  node_id UUID NOT NULL REFERENCES t_nodes(id),
  template_revision_id UUID NOT NULL REFERENCES t_business_metric_revisions(id),
  base_site_configuration_version BIGINT NOT NULL REFERENCES t_site_configuration_versions(version),
  frozen_timezone TEXT CHECK (frozen_timezone IS NULL OR btrim(frozen_timezone) <> ''),
  raw_detail_retention_days INTEGER CHECK (raw_detail_retention_days IS NULL OR raw_detail_retention_days >= 0),
  source_digest CHAR(64) CHECK (source_digest IS NULL OR source_digest ~ '^[0-9a-f]{64}$'),
  internal_processing_digest CHAR(64) CHECK (internal_processing_digest IS NULL OR internal_processing_digest ~ '^[0-9a-f]{64}$'),
  previous_installation_id UUID,
  status TEXT NOT NULL CHECK (status IN ('ready','blocked')),
  digest CHAR(64) NOT NULL UNIQUE CHECK (digest ~ '^[0-9a-f]{64}$'),
  planned_by TEXT NOT NULL CHECK (btrim(planned_by) <> ''),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (
    status = 'blocked'
    OR (
      frozen_timezone IS NOT NULL
      AND raw_detail_retention_days IS NOT NULL
      AND source_digest IS NOT NULL
      AND internal_processing_digest IS NOT NULL
    )
  )
);

CREATE TABLE IF NOT EXISTS t_business_metric_plan_items (
  plan_id UUID NOT NULL REFERENCES t_business_metric_installation_plans(id),
  item_key TEXT NOT NULL CHECK (btrim(item_key) <> ''),
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  item_kind TEXT NOT NULL CHECK (item_kind IN ('source','output','capability','blocker')),
  action TEXT NOT NULL CHECK (action IN ('add','reuse','update','preserve','block')),
  source_entity_instance_id UUID REFERENCES t_entity_instances(id),
  method TEXT CHECK (method IN ('counter_delta','power_integral','average','maximum')),
  estimated BOOLEAN,
  blocker_code TEXT,
  before_value JSONB,
  after_value JSONB,
  PRIMARY KEY(plan_id, item_key),
  UNIQUE(plan_id, ordinal),
  CHECK ((item_kind = 'source') = (source_entity_instance_id IS NOT NULL)),
  CHECK ((item_kind = 'blocker') = (blocker_code IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS t_installed_business_metrics (
  id UUID PRIMARY KEY,
  node_id UUID NOT NULL REFERENCES t_nodes(id),
  entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  template_revision_id UUID NOT NULL REFERENCES t_business_metric_revisions(id),
  installed_processing_id UUID NOT NULL REFERENCES t_installed_point_processings(id),
  source_plan_id UUID NOT NULL REFERENCES t_business_metric_installation_plans(id),
  site_configuration_version BIGINT NOT NULL REFERENCES t_site_configuration_versions(version),
  frozen_timezone TEXT NOT NULL CHECK (btrim(frozen_timezone) <> ''),
  raw_detail_retention_days INTEGER NOT NULL CHECK (raw_detail_retention_days >= 0),
  state TEXT NOT NULL CHECK (state IN ('active','disabled')),
  installed_by TEXT NOT NULL CHECK (btrim(installed_by) <> ''),
  idempotency_key TEXT NOT NULL CHECK (btrim(idempotency_key) <> ''),
  installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(node_id, entity_instance_id, template_revision_id),
  UNIQUE(id, entity_instance_id),
  UNIQUE(installed_by, idempotency_key)
);

ALTER TABLE t_business_metric_installation_plans
  DROP CONSTRAINT IF EXISTS fk_business_metric_plan_previous_installation;
ALTER TABLE t_business_metric_installation_plans
  ADD CONSTRAINT fk_business_metric_plan_previous_installation
  FOREIGN KEY(previous_installation_id)
  REFERENCES t_installed_business_metrics(id);

CREATE TABLE IF NOT EXISTS t_business_metric_source_bindings (
  installed_metric_id UUID NOT NULL REFERENCES t_installed_business_metrics(id),
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  entity_definition_id TEXT NOT NULL CHECK (btrim(entity_definition_id) <> ''),
  method TEXT NOT NULL CHECK (method IN ('counter_delta','power_integral','average','maximum')),
  data_type TEXT NOT NULL CHECK (data_type IN ('FLOAT','INT')),
  unit TEXT,
  direction TEXT NOT NULL CHECK (direction IN ('R','RW')),
  estimated BOOLEAN NOT NULL,
  source_digest CHAR(64) NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
  PRIMARY KEY(installed_metric_id, ordinal),
  UNIQUE(installed_metric_id, entity_instance_id)
);

CREATE TABLE IF NOT EXISTS t_business_metric_projections (
  installed_metric_id UUID PRIMARY KEY REFERENCES t_installed_business_metrics(id),
  window_started_at TIMESTAMPTZ NOT NULL,
  window_ended_at TIMESTAMPTZ NOT NULL CHECK (window_ended_at > window_started_at),
  watermark_at TIMESTAMPTZ,
  coverage DOUBLE PRECISION NOT NULL CHECK (coverage >= 0 AND coverage <= 1),
  quality SMALLINT NOT NULL CHECK (quality IN (0,64,192)),
  estimated BOOLEAN NOT NULL,
  state JSONB NOT NULL CHECK (jsonb_typeof(state) = 'object'),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_l2_event_observed_entity
  ON t_l2_observations(event_id, observed_at, entity_instance_id);

CREATE TABLE IF NOT EXISTS t_business_metric_window_results (
  installed_metric_id UUID NOT NULL REFERENCES t_installed_business_metrics(id),
  window_started_at TIMESTAMPTZ NOT NULL,
  window_ended_at TIMESTAMPTZ NOT NULL CHECK (window_ended_at > window_started_at),
  revision INTEGER NOT NULL CHECK (revision > 0),
  lifecycle TEXT NOT NULL CHECK (lifecycle IN ('completed','corrected','invalid')),
  calculation_method TEXT NOT NULL
    CHECK (calculation_method IN ('counter_delta','power_integral','average','maximum')),
  quality SMALLINT NOT NULL CHECK (quality IN (0,64,192)),
  coverage DOUBLE PRECISION NOT NULL CHECK (coverage >= 0 AND coverage <= 1),
  estimated BOOLEAN NOT NULL,
  source_count INTEGER NOT NULL CHECK (source_count >= 0),
  first_source_event_id UUID,
  first_source_observed_at TIMESTAMPTZ,
  last_source_event_id UUID,
  last_source_observed_at TIMESTAMPTZ,
  result_event_id UUID,
  result_observed_at TIMESTAMPTZ,
  result_entity_instance_id UUID,
  content_digest CHAR(64) NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
  source_summary JSONB NOT NULL CHECK (jsonb_typeof(source_summary) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(installed_metric_id, window_started_at, window_ended_at, revision),
  FOREIGN KEY(first_source_event_id, first_source_observed_at)
    REFERENCES t_l2_observations(event_id, observed_at),
  FOREIGN KEY(last_source_event_id, last_source_observed_at)
    REFERENCES t_l2_observations(event_id, observed_at),
  FOREIGN KEY(result_event_id, result_observed_at, result_entity_instance_id)
    REFERENCES t_l2_observations(event_id, observed_at, entity_instance_id),
  FOREIGN KEY(installed_metric_id, result_entity_instance_id)
    REFERENCES t_installed_business_metrics(id, entity_instance_id),
  CHECK ((first_source_event_id IS NULL) = (first_source_observed_at IS NULL)),
  CHECK ((last_source_event_id IS NULL) = (last_source_observed_at IS NULL)),
  CHECK (
    (source_count = 0
      AND first_source_event_id IS NULL AND first_source_observed_at IS NULL
      AND last_source_event_id IS NULL AND last_source_observed_at IS NULL)
    OR
    (source_count > 0
      AND first_source_event_id IS NOT NULL AND first_source_observed_at IS NOT NULL
      AND last_source_event_id IS NOT NULL AND last_source_observed_at IS NOT NULL)
  ),
  CHECK (
    (lifecycle IN ('completed','corrected')
      AND result_event_id IS NOT NULL AND result_observed_at IS NOT NULL
      AND result_entity_instance_id IS NOT NULL)
    OR (lifecycle = 'invalid' AND result_event_id IS NULL
      AND result_observed_at IS NULL AND result_entity_instance_id IS NULL)
  )
);

CREATE TABLE IF NOT EXISTS t_business_metric_recomputations (
  id UUID PRIMARY KEY,
  request_id UUID NOT NULL,
  revision INTEGER NOT NULL CHECK (revision > 0),
  installed_metric_id UUID NOT NULL REFERENCES t_installed_business_metrics(id),
  requested_by TEXT NOT NULL CHECK (btrim(requested_by) <> ''),
  approved_by TEXT,
  range_started_at TIMESTAMPTZ NOT NULL,
  range_ended_at TIMESTAMPTZ NOT NULL CHECK (range_ended_at > range_started_at),
  reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
  status TEXT NOT NULL CHECK (status IN ('requested','approved','running','completed','rejected','failed')),
  evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(request_id, revision)
);

CREATE TABLE IF NOT EXISTS t_entity_capability_contracts (
  id UUID PRIMARY KEY,
  entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  installed_metric_id UUID REFERENCES t_installed_business_metrics(id),
  temporal_semantics TEXT NOT NULL CHECK (temporal_semantics IN ('instant','windowed')),
  control_eligible BOOLEAN NOT NULL,
  content JSONB NOT NULL CHECK (jsonb_typeof(content) = 'object'),
  digest CHAR(64) NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(entity_instance_id, digest)
);

CREATE TABLE IF NOT EXISTS t_business_metric_audit (
  id UUID PRIMARY KEY,
  installed_metric_id UUID REFERENCES t_installed_business_metrics(id),
  plan_id UUID REFERENCES t_business_metric_installation_plans(id),
  action TEXT NOT NULL CHECK (action IN ('installed','upgraded','reused','disabled','enabled','recomputed','rejected')),
  actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
  idempotency_key TEXT CHECK (idempotency_key IS NULL OR btrim(idempotency_key) <> ''),
  request_digest CHAR(64) CHECK (request_digest IS NULL OR request_digest ~ '^[0-9a-f]{64}$'),
  resulting_state TEXT CHECK (resulting_state IS NULL OR resulting_state IN ('active','disabled')),
  evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
  digest CHAR(64) NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (
    (action = 'disabled' AND resulting_state = 'disabled')
    OR (action IN ('installed','upgraded','reused','enabled') AND resulting_state = 'active')
    OR (action IN ('recomputed','rejected') AND resulting_state IS NULL)
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_business_metric_audit_idempotency
ON t_business_metric_audit(actor, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS t_business_metric_acceptance_reports (
  id UUID PRIMARY KEY,
  installed_metric_id UUID NOT NULL REFERENCES t_installed_business_metrics(id),
  window_result_installed_metric_id UUID NOT NULL,
  window_result_started_at TIMESTAMPTZ NOT NULL,
  window_result_ended_at TIMESTAMPTZ NOT NULL,
  window_result_revision INTEGER NOT NULL,
  runtime_instance_id UUID NOT NULL REFERENCES t_runtime_instances(id),
  schema_version TEXT NOT NULL CHECK (schema_version = '043'),
  status TEXT NOT NULL CHECK (status IN ('passed','failed')),
  report JSONB NOT NULL CHECK (jsonb_typeof(report) = 'object'),
  digest CHAR(64) NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY(window_result_installed_metric_id, window_result_started_at, window_result_ended_at, window_result_revision)
    REFERENCES t_business_metric_window_results(installed_metric_id, window_started_at, window_ended_at, revision),
  CHECK (window_result_installed_metric_id = installed_metric_id),
  UNIQUE(installed_metric_id, digest)
);

CREATE INDEX IF NOT EXISTS ix_business_metric_installed_node ON t_installed_business_metrics(node_id, state);
CREATE INDEX IF NOT EXISTS ix_business_metric_source_bindings_entity ON t_business_metric_source_bindings(entity_instance_id);
CREATE INDEX IF NOT EXISTS ix_business_metric_window_results_lookup ON t_business_metric_window_results(installed_metric_id, window_ended_at DESC, revision DESC);

CREATE OR REPLACE FUNCTION guard_business_metric_projection()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.installed_metric_id IS DISTINCT FROM OLD.installed_metric_id
       OR NEW.window_started_at IS DISTINCT FROM OLD.window_started_at
       OR NEW.window_ended_at IS DISTINCT FROM OLD.window_ended_at THEN
      RAISE EXCEPTION 'business metric projection identity is immutable'
        USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'business metric projection rows cannot be deleted or truncated'
    USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS trg_business_metric_projection_guard
  ON t_business_metric_projections;
CREATE TRIGGER trg_business_metric_projection_guard
BEFORE UPDATE OR DELETE ON t_business_metric_projections
FOR EACH ROW EXECUTE FUNCTION guard_business_metric_projection();

DROP TRIGGER IF EXISTS trg_business_metric_projection_no_truncate
  ON t_business_metric_projections;
CREATE TRIGGER trg_business_metric_projection_no_truncate
BEFORE TRUNCATE ON t_business_metric_projections
FOR EACH STATEMENT EXECUTE FUNCTION guard_business_metric_projection();

DO $$
DECLARE
  table_name TEXT;
  immutable_tables TEXT[] := ARRAY[
    't_business_metric_templates',
    't_business_metric_revisions',
    't_business_metric_installation_plans',
    't_business_metric_plan_items',
    't_installed_business_metrics',
    't_business_metric_source_bindings',
    't_business_metric_window_results',
    't_business_metric_recomputations',
    't_entity_capability_contracts',
    't_business_metric_audit',
    't_business_metric_acceptance_reports'
  ];
BEGIN
  FOREACH table_name IN ARRAY immutable_tables LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || table_name || '_immutable', table_name);
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_data_trunk_append_only()', 'trg_' || table_name || '_immutable', table_name);
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'trg_' || table_name || '_no_truncate', table_name);
    EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON %I FOR EACH STATEMENT EXECUTE FUNCTION reject_data_trunk_append_only()', 'trg_' || table_name || '_no_truncate', table_name);
  END LOOP;
END;
$$;
