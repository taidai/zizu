-- Schema 043: immutable business-metric delivery records and recoverable projections.
-- This migration is expand-only over the complete Schema 042 data trunk.

DO $$
DECLARE
  existing_tables INTEGER;
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
  IF to_regclass('public.t_point_processing_revisions') IS NULL
     OR to_regclass('public.t_installed_point_processings') IS NULL
     OR to_regclass('public.t_l2_observations') IS NULL
     OR to_regclass('public.t_site_configuration_versions') IS NULL THEN
    RAISE EXCEPTION 'schema 043 requires a complete schema 042' USING ERRCODE = '55000';
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
  DROP CONSTRAINT IF EXISTS chk_installed_point_processing_scope;
ALTER TABLE t_installed_point_processings
  ADD CONSTRAINT chk_installed_point_processing_scope
  CHECK (processing_scope IN ('node','business_metric'));

DROP INDEX IF EXISTS uq_installed_point_conversion_current;
DROP INDEX IF EXISTS uq_installed_point_processing_current;
CREATE UNIQUE INDEX uq_installed_point_processing_current
ON t_installed_point_processings(node_id)
WHERE current = TRUE AND processing_scope = 'node';

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
  action TEXT NOT NULL CHECK (action IN ('add','reuse','preserve','block')),
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
  UNIQUE(installed_by, idempotency_key)
);

CREATE TABLE IF NOT EXISTS t_business_metric_source_bindings (
  installed_metric_id UUID NOT NULL REFERENCES t_installed_business_metrics(id),
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  entity_definition_id TEXT NOT NULL CHECK (btrim(entity_definition_id) <> ''),
  method TEXT NOT NULL CHECK (method IN ('counter_delta','power_integral','average','maximum')),
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

CREATE TABLE IF NOT EXISTS t_business_metric_window_results (
  installed_metric_id UUID NOT NULL REFERENCES t_installed_business_metrics(id),
  window_started_at TIMESTAMPTZ NOT NULL,
  window_ended_at TIMESTAMPTZ NOT NULL CHECK (window_ended_at > window_started_at),
  revision INTEGER NOT NULL CHECK (revision > 0),
  lifecycle TEXT NOT NULL CHECK (lifecycle IN ('provisional','completed','corrected','invalid')),
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
  content_digest CHAR(64) NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
  source_summary JSONB NOT NULL CHECK (jsonb_typeof(source_summary) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(installed_metric_id, window_started_at, window_ended_at, revision),
  FOREIGN KEY(result_event_id, result_observed_at)
    REFERENCES t_l2_observations(event_id, observed_at),
  CHECK ((first_source_event_id IS NULL) = (first_source_observed_at IS NULL)),
  CHECK ((last_source_event_id IS NULL) = (last_source_observed_at IS NULL)),
  CHECK (
    (lifecycle IN ('completed','corrected') AND result_event_id IS NOT NULL AND result_observed_at IS NOT NULL)
    OR (lifecycle = 'invalid' AND result_event_id IS NULL AND result_observed_at IS NULL)
    OR lifecycle = 'provisional'
  )
);

CREATE TABLE IF NOT EXISTS t_business_metric_recomputations (
  id UUID PRIMARY KEY,
  installed_metric_id UUID NOT NULL REFERENCES t_installed_business_metrics(id),
  requested_by TEXT NOT NULL CHECK (btrim(requested_by) <> ''),
  approved_by TEXT,
  range_started_at TIMESTAMPTZ NOT NULL,
  range_ended_at TIMESTAMPTZ NOT NULL CHECK (range_ended_at > range_started_at),
  reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
  status TEXT NOT NULL CHECK (status IN ('requested','approved','running','completed','rejected','failed')),
  evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
  action TEXT NOT NULL CHECK (action IN ('installed','upgraded','disabled','enabled','recomputed','rejected')),
  actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
  evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence) = 'object'),
  digest CHAR(64) NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS t_business_metric_acceptance_reports (
  id UUID PRIMARY KEY,
  installed_metric_id UUID NOT NULL REFERENCES t_installed_business_metrics(id),
  window_result_installed_metric_id UUID,
  window_result_started_at TIMESTAMPTZ,
  window_result_ended_at TIMESTAMPTZ,
  window_result_revision INTEGER,
  runtime_instance_id UUID REFERENCES t_runtime_instances(id),
  schema_version TEXT NOT NULL CHECK (schema_version = '043'),
  status TEXT NOT NULL CHECK (status IN ('passed','failed')),
  report JSONB NOT NULL CHECK (jsonb_typeof(report) = 'object'),
  digest CHAR(64) NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY(window_result_installed_metric_id, window_result_started_at, window_result_ended_at, window_result_revision)
    REFERENCES t_business_metric_window_results(installed_metric_id, window_started_at, window_ended_at, revision),
  CHECK (
    (window_result_installed_metric_id IS NULL AND window_result_started_at IS NULL AND window_result_ended_at IS NULL AND window_result_revision IS NULL)
    OR (window_result_installed_metric_id IS NOT NULL AND window_result_started_at IS NOT NULL AND window_result_ended_at IS NOT NULL AND window_result_revision IS NOT NULL)
  ),
  UNIQUE(installed_metric_id, digest)
);

CREATE INDEX IF NOT EXISTS ix_business_metric_installed_node ON t_installed_business_metrics(node_id, state);
CREATE INDEX IF NOT EXISTS ix_business_metric_source_bindings_entity ON t_business_metric_source_bindings(entity_instance_id);
CREATE INDEX IF NOT EXISTS ix_business_metric_window_results_lookup ON t_business_metric_window_results(installed_metric_id, window_ended_at DESC, revision DESC);

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
