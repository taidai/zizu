-- Schema 042: typed cross-node formulas, frozen selectors, and site DAG evidence.

DO $$
DECLARE
  existing_tables INTEGER;
BEGIN
  SELECT count(*) INTO existing_tables
  FROM (VALUES
    ('t_point_processing_expressions'),
    ('t_point_processing_selectors'),
    ('t_point_processing_selector_members'),
    ('t_point_processing_dependencies')
  ) AS expected(name)
  WHERE to_regclass('public.' || expected.name) IS NOT NULL;

  IF existing_tables NOT IN (0, 4) THEN
    RAISE EXCEPTION 'schema 042 is partially present'
      USING ERRCODE = '55000';
  END IF;

  IF to_regclass('public.t_point_processing_outputs') IS NULL
     OR to_regclass('public.t_point_processing_inputs') IS NULL
     OR to_regclass('public.t_installed_point_processings') IS NULL
     OR to_regclass('public.t_l2_latest') IS NULL
     OR to_regclass('public.t_runtime_health_samples') IS NULL THEN
    RAISE EXCEPTION 'schema 042 requires a complete schema 041'
      USING ERRCODE = '55000';
  END IF;
END;
$$;

ALTER TABLE t_nodes
  ADD COLUMN IF NOT EXISTS parent_id UUID REFERENCES t_nodes(id);

CREATE INDEX IF NOT EXISTS ix_nodes_parent_id
  ON t_nodes(parent_id);

CREATE TABLE IF NOT EXISTS t_point_processing_expressions (
  output_id UUID PRIMARY KEY REFERENCES t_point_processing_outputs(id),
  dsl_text TEXT NOT NULL CHECK (btrim(dsl_text) <> ''),
  canonical_ast JSONB NOT NULL CHECK (jsonb_typeof(canonical_ast) = 'object'),
  ast_digest CHAR(64) NOT NULL CHECK (ast_digest ~ '^[0-9a-f]{64}$'),
  result_data_type TEXT NOT NULL
    CHECK (result_data_type IN ('FLOAT','INT','BOOL')),
  result_unit TEXT,
  schedule_seconds INTEGER NOT NULL
    CHECK (schedule_seconds BETWEEN 1 AND 3600),
  control_eligible BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS t_point_processing_selectors (
  input_id UUID PRIMARY KEY REFERENCES t_point_processing_inputs(id),
  scope TEXT NOT NULL CHECK (scope = 'descendants'),
  node_type TEXT NOT NULL CHECK (btrim(node_type) <> ''),
  entity_definition_id TEXT NOT NULL CHECK (btrim(entity_definition_id) <> ''),
  cardinality TEXT NOT NULL CHECK (cardinality IN ('one','many')),
  default_value JSONB,
  CONSTRAINT chk_point_processing_selector_default_scalar CHECK (
    default_value IS NULL
    OR jsonb_typeof(default_value) IN ('number','boolean')
  )
);

CREATE TABLE IF NOT EXISTS t_point_processing_selector_members (
  installed_processing_id UUID NOT NULL
    REFERENCES t_installed_point_processings(id),
  input_id UUID NOT NULL REFERENCES t_point_processing_inputs(id),
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  selector_digest CHAR(64) NOT NULL
    CHECK (selector_digest ~ '^[0-9a-f]{64}$'),
  PRIMARY KEY(installed_processing_id, input_id, ordinal),
  UNIQUE(installed_processing_id, input_id, entity_instance_id)
);

CREATE TABLE IF NOT EXISTS t_point_processing_dependencies (
  installed_processing_id UUID NOT NULL
    REFERENCES t_installed_point_processings(id),
  input_id UUID NOT NULL REFERENCES t_point_processing_inputs(id),
  output_id UUID NOT NULL REFERENCES t_point_processing_outputs(id),
  source_entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  target_entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
  PRIMARY KEY(
    installed_processing_id,
    input_id,
    output_id,
    source_entity_instance_id,
    target_entity_instance_id
  ),
  CONSTRAINT chk_point_processing_dependency_not_self CHECK (
    source_entity_instance_id <> target_entity_instance_id
  )
);

CREATE INDEX IF NOT EXISTS ix_point_processing_dependencies_source
  ON t_point_processing_dependencies(source_entity_instance_id);
CREATE INDEX IF NOT EXISTS ix_point_processing_dependencies_target
  ON t_point_processing_dependencies(target_entity_instance_id);
CREATE INDEX IF NOT EXISTS ix_point_processing_selector_members_entity
  ON t_point_processing_selector_members(entity_instance_id);

CREATE TABLE IF NOT EXISTS t_point_processing_formula_runs (
  installed_processing_id UUID NOT NULL
    REFERENCES t_installed_point_processings(id),
  output_id UUID NOT NULL REFERENCES t_point_processing_outputs(id),
  last_evaluated_at TIMESTAMPTZ NOT NULL,
  last_event_id UUID,
  PRIMARY KEY(installed_processing_id, output_id)
);

DROP TRIGGER IF EXISTS trg_point_processing_expressions_immutable
  ON t_point_processing_expressions;
CREATE TRIGGER trg_point_processing_expressions_immutable
BEFORE UPDATE OR DELETE ON t_point_processing_expressions
FOR EACH ROW EXECUTE FUNCTION reject_data_trunk_append_only();

DROP TRIGGER IF EXISTS trg_point_processing_expressions_no_truncate
  ON t_point_processing_expressions;
CREATE TRIGGER trg_point_processing_expressions_no_truncate
BEFORE TRUNCATE ON t_point_processing_expressions
FOR EACH STATEMENT EXECUTE FUNCTION reject_data_trunk_append_only();

DROP TRIGGER IF EXISTS trg_point_processing_selectors_immutable
  ON t_point_processing_selectors;
CREATE TRIGGER trg_point_processing_selectors_immutable
BEFORE UPDATE OR DELETE ON t_point_processing_selectors
FOR EACH ROW EXECUTE FUNCTION reject_data_trunk_append_only();

DROP TRIGGER IF EXISTS trg_point_processing_selectors_no_truncate
  ON t_point_processing_selectors;
CREATE TRIGGER trg_point_processing_selectors_no_truncate
BEFORE TRUNCATE ON t_point_processing_selectors
FOR EACH STATEMENT EXECUTE FUNCTION reject_data_trunk_append_only();

DROP TRIGGER IF EXISTS trg_point_processing_selector_members_immutable
  ON t_point_processing_selector_members;
CREATE TRIGGER trg_point_processing_selector_members_immutable
BEFORE UPDATE OR DELETE ON t_point_processing_selector_members
FOR EACH ROW EXECUTE FUNCTION reject_data_trunk_append_only();

DROP TRIGGER IF EXISTS trg_point_processing_selector_members_no_truncate
  ON t_point_processing_selector_members;
CREATE TRIGGER trg_point_processing_selector_members_no_truncate
BEFORE TRUNCATE ON t_point_processing_selector_members
FOR EACH STATEMENT EXECUTE FUNCTION reject_data_trunk_append_only();

DROP TRIGGER IF EXISTS trg_point_processing_dependencies_immutable
  ON t_point_processing_dependencies;
CREATE TRIGGER trg_point_processing_dependencies_immutable
BEFORE UPDATE OR DELETE ON t_point_processing_dependencies
FOR EACH ROW EXECUTE FUNCTION reject_data_trunk_append_only();

DROP TRIGGER IF EXISTS trg_point_processing_dependencies_no_truncate
  ON t_point_processing_dependencies;
CREATE TRIGGER trg_point_processing_dependencies_no_truncate
BEFORE TRUNCATE ON t_point_processing_dependencies
FOR EACH STATEMENT EXECUTE FUNCTION reject_data_trunk_append_only();
