-- Schema 043: immutable business-metric delivery records and recoverable projections.
-- This migration is expand-only over the complete Schema 042 data trunk.

DO $$
DECLARE
  existing_tables INTEGER;
  existing_contract_columns INTEGER;
  schema_042_tables INTEGER;
  schema_042_contract_columns INTEGER;
  schema_042_recorded BOOLEAN;
  schema_043_contract_triggers INTEGER;
  schema_043_expected_columns INTEGER;
  schema_043_primary_keys INTEGER;
  schema_043_immutable_triggers INTEGER;
  schema_043_constraints INTEGER;
  schema_043_expected_constraints INTEGER;
  schema_043_extension_columns INTEGER;
  schema_043_extension_footprint INTEGER;
  schema_043_extension_constraints INTEGER;
  schema_043_extension_indexes INTEGER;
  schema_043_expected_contract_triggers INTEGER;
  schema_043_constraint_definitions INTEGER;
  schema_043_expected_constraint_definitions INTEGER;
  schema_043_function_contracts INTEGER;
  schema_043_expected_function_contracts INTEGER;
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

  SELECT count(*) INTO schema_043_extension_columns
  FROM (VALUES
    ('t_point_processing_revisions', 'internal_kind'),
    ('t_installed_point_processings', 'processing_scope'),
    ('t_installed_point_processings', 'processing_owner_key')
  ) AS expected(table_name, column_name)
  WHERE EXISTS (
    SELECT 1
    FROM information_schema.columns AS columns
    WHERE columns.table_schema = 'public'
      AND columns.table_name = expected.table_name
      AND columns.column_name = expected.column_name
  );
  SELECT count(*) INTO schema_043_extension_constraints
  FROM (VALUES
    ('t_point_processing_revisions',
     'chk_point_processing_revision_internal_kind'),
    ('t_installed_point_processings',
     'chk_installed_point_processing_scope')
  ) AS expected(table_name, constraint_name)
  WHERE EXISTS (
    SELECT 1
    FROM pg_catalog.pg_constraint AS constraint_record
    JOIN pg_catalog.pg_class AS relation
      ON relation.oid = constraint_record.conrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public'
      AND relation.relname = expected.table_name
      AND constraint_record.conname = expected.constraint_name
  );
  SELECT count(*) INTO schema_043_extension_indexes
  FROM (VALUES ('uq_installed_business_metric_processing_current')) AS expected(name)
  WHERE to_regclass('public.' || expected.name) IS NOT NULL;
  schema_043_extension_footprint :=
    schema_043_extension_columns
    + schema_043_extension_constraints
    + schema_043_extension_indexes;

  IF existing_tables NOT IN (0, 12) THEN
    RAISE EXCEPTION 'SCHEMA_043_PARTIAL_STRUCTURE: metric tables are incomplete'
      USING ERRCODE = '55000';
  END IF;
  IF existing_tables = 0 AND schema_043_extension_footprint <> 0 THEN
    RAISE EXCEPTION 'SCHEMA_043_PARTIAL_STRUCTURE: point-processing extension is partial'
      USING ERRCODE = '55000';
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

  IF to_regclass('public.schema_migrations') IS NOT NULL THEN
    EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.schema_migrations WHERE version = $1)'
      INTO schema_042_recorded USING '042';
    IF NOT schema_042_recorded THEN
      RAISE EXCEPTION 'schema 043 requires recorded schema 042 migration evidence'
        USING ERRCODE = '55000';
    END IF;
  END IF;

  SELECT count(*) INTO schema_042_contract_columns
  FROM (VALUES
    ('t_nodes', 'parent_id', 'uuid', FALSE),
    ('t_point_processing_revisions', 'id', 'uuid', TRUE),
    ('t_point_processing_revisions', 'template_id', 'uuid', TRUE),
    ('t_point_processing_revisions', 'revision', 'integer', TRUE),
    ('t_point_processing_revisions', 'content_digest', 'character', TRUE),
    ('t_installed_point_processings', 'id', 'uuid', TRUE),
    ('t_installed_point_processings', 'node_id', 'uuid', TRUE),
    ('t_installed_point_processings', 'revision_id', 'uuid', TRUE),
    ('t_installed_point_processings', 'current', 'boolean', TRUE),
    ('t_point_processing_expressions', 'output_id', 'uuid', TRUE),
    ('t_point_processing_expressions', 'canonical_ast', 'jsonb', TRUE),
    ('t_point_processing_selectors', 'input_id', 'uuid', TRUE),
    ('t_point_processing_selector_members', 'installed_processing_id', 'uuid', TRUE),
    ('t_point_processing_dependencies', 'installed_processing_id', 'uuid', TRUE),
    ('t_point_processing_formula_runs', 'installed_processing_id', 'uuid', TRUE),
    ('t_cross_node_processing_acceptance_reports', 'id', 'uuid', TRUE),
    ('t_l2_observations', 'event_id', 'uuid', TRUE),
    ('t_l2_observations', 'observed_at', 'timestamp with time zone', TRUE),
      ('t_l2_observations', 'entity_instance_id', 'uuid', TRUE),
      ('t_l2_observations', 'producing_runtime_instance_id', 'uuid', FALSE),
    ('t_runtime_instances', 'id', 'uuid', TRUE),
    ('t_site_configuration_state', 'current_version', 'bigint', TRUE)
  ) AS required(table_name, column_name, type_name, required_not_null)
  JOIN pg_namespace AS namespace ON namespace.nspname = 'public'
  JOIN pg_class AS relation
    ON relation.relnamespace = namespace.oid
   AND relation.relname = required.table_name
  JOIN pg_attribute AS attribute
    ON attribute.attrelid = relation.oid
   AND attribute.attname = required.column_name
   AND attribute.attnum > 0
   AND NOT attribute.attisdropped
  WHERE attribute.atttypid = required.type_name::regtype
    AND attribute.attnotnull = required.required_not_null;

  IF schema_042_contract_columns <> 22
     OR NOT EXISTS (
       SELECT 1 FROM pg_catalog.pg_constraint
       WHERE conrelid = 'public.t_point_processing_expressions'::regclass
         AND contype = 'p'
     )
     OR to_regclass('public.uq_l2_event_observed_at') IS NULL
     OR to_regclass('public.ix_nodes_parent_id') IS NULL
     OR NOT EXISTS (
       SELECT 1 FROM pg_catalog.pg_trigger
       WHERE tgrelid = 'public.t_point_processing_expressions'::regclass
         AND tgname = 'trg_point_processing_expressions_immutable'
         AND NOT tgisinternal
     ) THEN
    RAISE EXCEPTION 'schema 042 structure is malformed'
      USING ERRCODE = '55000';
  END IF;

  IF existing_tables = 12 THEN
    SELECT count(*) INTO schema_043_extension_columns
    FROM (VALUES
      ('t_point_processing_revisions', 'internal_kind', 'text', FALSE, NULL::TEXT),
      ('t_installed_point_processings', 'processing_scope', 'text', TRUE, '''node''::text'),
      ('t_installed_point_processings', 'processing_owner_key', 'uuid', FALSE, NULL::TEXT)
    ) AS required(
      table_name, column_name, type_name, required_not_null, default_expression
    )
    WHERE EXISTS (
      SELECT 1
      FROM pg_namespace AS namespace
      JOIN pg_class AS relation
        ON relation.relnamespace = namespace.oid
      JOIN pg_attribute AS attribute
        ON attribute.attrelid = relation.oid
       AND attribute.attname = required.column_name
       AND attribute.attnum > 0
       AND NOT attribute.attisdropped
      LEFT JOIN pg_attrdef AS column_default
        ON column_default.adrelid = relation.oid
       AND column_default.adnum = attribute.attnum
      WHERE namespace.nspname = 'public'
        AND relation.relname = required.table_name
        AND attribute.atttypid = required.type_name::regtype
        AND attribute.attnotnull = required.required_not_null
        AND pg_get_expr(column_default.adbin, column_default.adrelid)
            IS NOT DISTINCT FROM required.default_expression
    );

    SELECT count(*) INTO schema_043_extension_constraints
    FROM (VALUES
      (
        't_point_processing_revisions',
        'chk_point_processing_revision_internal_kind',
        '((internal_kind IS NULL) OR (internal_kind = ''business_metric''::text))'
      ),
      (
        't_installed_point_processings',
        'chk_installed_point_processing_scope',
        '(((processing_scope = ''node''::text) AND (processing_owner_key IS NULL)) OR ((processing_scope = ''business_metric''::text) AND (processing_owner_key IS NOT NULL)))'
      )
    ) AS required(table_name, constraint_name, check_expression)
    WHERE EXISTS (
      SELECT 1
      FROM pg_catalog.pg_constraint AS constraint_record
      WHERE constraint_record.conrelid = to_regclass(
              'public.' || required.table_name
            )
        AND constraint_record.conname = required.constraint_name
        AND constraint_record.contype = 'c'
        AND pg_get_expr(
              constraint_record.conbin, constraint_record.conrelid
            ) = required.check_expression
    );

    SELECT count(*) INTO schema_043_extension_indexes
    FROM (VALUES
      (
        'uq_installed_point_processing_current',
        'CREATE UNIQUE INDEX uq_installed_point_processing_current ON public.t_installed_point_processings USING btree (node_id) WHERE ((current = true) AND (processing_scope = ''node''::text))'
      ),
      (
        'uq_installed_business_metric_processing_current',
        'CREATE UNIQUE INDEX uq_installed_business_metric_processing_current ON public.t_installed_point_processings USING btree (node_id, processing_owner_key) WHERE ((current = true) AND (processing_scope = ''business_metric''::text))'
      )
    ) AS required(index_name, index_definition)
    WHERE EXISTS (
      SELECT 1
      FROM pg_index AS index_record
      WHERE index_record.indexrelid = to_regclass(
              'public.' || required.index_name
            )
        AND index_record.indisunique
        AND pg_get_indexdef(index_record.indexrelid) = required.index_definition
    );

    IF schema_043_extension_columns <> 3
       OR schema_043_extension_constraints <> 2
       OR schema_043_extension_indexes <> 2 THEN
      RAISE EXCEPTION 'SCHEMA_043_PARTIAL_STRUCTURE: point-processing extension is malformed'
        USING ERRCODE = '55000';
    END IF;

    SELECT count(*), count(*) FILTER (WHERE EXISTS (
      SELECT 1
      FROM pg_namespace AS namespace
      JOIN pg_class AS relation
        ON relation.relnamespace = namespace.oid
      JOIN pg_attribute AS attribute
        ON attribute.attrelid = relation.oid
       AND attribute.attname = required.column_name
       AND attribute.attnum > 0
       AND NOT attribute.attisdropped
      WHERE namespace.nspname = 'public'
        AND relation.relname = required.table_name
        AND attribute.atttypid = required.type_name::regtype
        AND attribute.attnotnull = required.required_not_null
    )) INTO schema_043_expected_columns, existing_contract_columns
    FROM (VALUES
      ('t_l2_observations', 'event_time_basis', 'text', TRUE),
      ('t_l2_observations', 'commit_sequence', 'bigint', TRUE),
      ('t_l2_observations', 'value_numeric', 'numeric', FALSE),
      ('t_l2_latest', 'event_time_basis', 'text', TRUE),
      ('t_l2_latest', 'value_numeric', 'numeric', FALSE),
      ('t_l2_observation_sources', 'source_event_time_basis', 'text', TRUE),
      ('t_tags', 'timestamp_trusted', 'boolean', TRUE),
      ('t_telemetry', 'event_time_basis', 'text', TRUE),
      ('t_telemetry', 'event_received_at', 'timestamp with time zone', TRUE),
      ('t_telemetry_latest', 'event_time_basis', 'text', TRUE),
      ('t_telemetry_latest', 'event_received_at', 'timestamp with time zone', TRUE),
      ('t_business_metric_templates', 'id', 'uuid', TRUE),
      ('t_business_metric_templates', 'template_key', 'text', TRUE),
      ('t_business_metric_revisions', 'id', 'uuid', TRUE),
      ('t_business_metric_revisions', 'template_id', 'uuid', TRUE),
      ('t_business_metric_revisions', 'revision', 'integer', TRUE),
      ('t_business_metric_revisions', 'content', 'jsonb', TRUE),
      ('t_business_metric_revisions', 'content_digest', 'character', TRUE),
      ('t_business_metric_installation_plans', 'id', 'uuid', TRUE),
      ('t_business_metric_installation_plans', 'node_id', 'uuid', TRUE),
      ('t_business_metric_installation_plans', 'template_revision_id', 'uuid', TRUE),
      ('t_business_metric_installation_plans', 'base_site_configuration_version', 'bigint', TRUE),
      ('t_business_metric_installation_plans', 'previous_installation_id', 'uuid', FALSE),
      ('t_business_metric_installation_plans', 'status', 'text', TRUE),
      ('t_business_metric_installation_plans', 'digest', 'character', TRUE),
      ('t_business_metric_plan_items', 'plan_id', 'uuid', TRUE),
      ('t_business_metric_plan_items', 'ordinal', 'integer', TRUE),
      ('t_business_metric_plan_items', 'item_kind', 'text', TRUE),
      ('t_business_metric_plan_items', 'action', 'text', TRUE),
      ('t_business_metric_plan_items', 'before_value', 'jsonb', FALSE),
      ('t_business_metric_plan_items', 'after_value', 'jsonb', FALSE),
      ('t_installed_business_metrics', 'id', 'uuid', TRUE),
      ('t_installed_business_metrics', 'node_id', 'uuid', TRUE),
      ('t_installed_business_metrics', 'entity_instance_id', 'uuid', TRUE),
      ('t_installed_business_metrics', 'template_revision_id', 'uuid', TRUE),
      ('t_installed_business_metrics', 'installed_processing_id', 'uuid', TRUE),
      ('t_installed_business_metrics', 'source_plan_id', 'uuid', TRUE),
      ('t_installed_business_metrics', 'site_configuration_version', 'bigint', TRUE),
      ('t_installed_business_metrics', 'state', 'text', TRUE),
      ('t_installed_business_metrics', 'installation_revision', 'integer', TRUE),
      ('t_installed_business_metrics', 'previous_installation_id', 'uuid', FALSE),
      ('t_business_metric_source_bindings', 'installed_metric_id', 'uuid', TRUE),
      ('t_business_metric_source_bindings', 'ordinal', 'integer', TRUE),
      ('t_business_metric_source_bindings', 'entity_instance_id', 'uuid', TRUE),
      ('t_business_metric_source_bindings', 'method', 'text', TRUE),
      ('t_business_metric_source_bindings', 'data_type', 'text', TRUE),
      ('t_business_metric_source_bindings', 'unit', 'text', FALSE),
      ('t_business_metric_source_bindings', 'direction', 'text', TRUE),
      ('t_business_metric_source_bindings', 'maximum_sample_gap_seconds', 'integer', TRUE),
      ('t_business_metric_source_bindings', 'producer_contract_digest', 'character', TRUE),
      ('t_business_metric_source_bindings', 'counter_maximum', 'numeric', FALSE),
      ('t_business_metric_source_bindings', 'counter_bit_width', 'smallint', FALSE),
      ('t_business_metric_source_bindings', 'counter_reset_on_decrease', 'boolean', FALSE),
      ('t_business_metric_source_bindings', 'counter_rollover_on_decrease', 'boolean', FALSE),
      ('t_business_metric_projections', 'installed_metric_id', 'uuid', TRUE),
      ('t_business_metric_projections', 'window_started_at', 'timestamp with time zone', TRUE),
      ('t_business_metric_projections', 'window_ended_at', 'timestamp with time zone', TRUE),
      ('t_business_metric_projections', 'watermark_at', 'timestamp with time zone', FALSE),
      ('t_business_metric_projections', 'coverage', 'double precision', TRUE),
      ('t_business_metric_projections', 'quality', 'smallint', TRUE),
      ('t_business_metric_projections', 'last_commit_sequence', 'bigint', TRUE),
      ('t_business_metric_projections', 'state', 'jsonb', TRUE),
      ('t_business_metric_window_results', 'installed_metric_id', 'uuid', TRUE),
      ('t_business_metric_window_results', 'window_started_at', 'timestamp with time zone', TRUE),
      ('t_business_metric_window_results', 'window_ended_at', 'timestamp with time zone', TRUE),
      ('t_business_metric_window_results', 'revision', 'integer', TRUE),
      ('t_business_metric_window_results', 'lifecycle', 'text', TRUE),
      ('t_business_metric_window_results', 'calculation_method', 'text', TRUE),
      ('t_business_metric_window_results', 'source_count', 'integer', TRUE),
      ('t_business_metric_window_results', 'first_source_event_id', 'uuid', FALSE),
      ('t_business_metric_window_results', 'first_source_observed_at', 'timestamp with time zone', FALSE),
      ('t_business_metric_window_results', 'first_source_effective_at', 'timestamp with time zone', FALSE),
      ('t_business_metric_window_results', 'last_source_event_id', 'uuid', FALSE),
      ('t_business_metric_window_results', 'last_source_observed_at', 'timestamp with time zone', FALSE),
      ('t_business_metric_window_results', 'last_source_effective_at', 'timestamp with time zone', FALSE),
      ('t_business_metric_window_results', 'result_event_id', 'uuid', FALSE),
      ('t_business_metric_window_results', 'result_observed_at', 'timestamp with time zone', FALSE),
      ('t_business_metric_window_results', 'result_entity_instance_id', 'uuid', FALSE),
      ('t_business_metric_recomputations', 'id', 'uuid', TRUE),
      ('t_business_metric_recomputations', 'request_id', 'uuid', TRUE),
      ('t_business_metric_recomputations', 'revision', 'integer', TRUE),
      ('t_business_metric_recomputations', 'installed_metric_id', 'uuid', TRUE),
      ('t_business_metric_recomputations', 'status', 'text', TRUE),
      ('t_entity_capability_contracts', 'id', 'uuid', TRUE),
      ('t_entity_capability_contracts', 'entity_instance_id', 'uuid', TRUE),
      ('t_entity_capability_contracts', 'installed_metric_id', 'uuid', FALSE),
      ('t_entity_capability_contracts', 'temporal_semantics', 'text', TRUE),
      ('t_entity_capability_contracts', 'content', 'jsonb', TRUE),
      ('t_business_metric_audit', 'id', 'uuid', TRUE),
      ('t_business_metric_audit', 'installed_metric_id', 'uuid', FALSE),
      ('t_business_metric_audit', 'plan_id', 'uuid', FALSE),
      ('t_business_metric_audit', 'action', 'text', TRUE),
      ('t_business_metric_audit', 'request_digest', 'character', FALSE),
      ('t_business_metric_audit', 'resulting_state', 'text', FALSE),
      ('t_business_metric_acceptance_reports', 'id', 'uuid', TRUE),
      ('t_business_metric_acceptance_reports', 'installed_metric_id', 'uuid', TRUE),
      ('t_business_metric_acceptance_reports', 'window_result_installed_metric_id', 'uuid', TRUE),
      ('t_business_metric_acceptance_reports', 'window_result_revision', 'integer', TRUE),
      ('t_business_metric_acceptance_reports', 'runtime_instance_id', 'uuid', TRUE),
      ('t_business_metric_acceptance_reports', 'schema_version', 'text', TRUE)
    ) AS required(table_name, column_name, type_name, required_not_null);

    SELECT count(*) INTO schema_043_primary_keys
    FROM pg_catalog.pg_constraint
    WHERE contype = 'p'
      AND conrelid IN (
        'public.t_business_metric_templates'::regclass,
        'public.t_business_metric_revisions'::regclass,
        'public.t_business_metric_installation_plans'::regclass,
        'public.t_business_metric_plan_items'::regclass,
        'public.t_installed_business_metrics'::regclass,
        'public.t_business_metric_source_bindings'::regclass,
        'public.t_business_metric_projections'::regclass,
        'public.t_business_metric_window_results'::regclass,
        'public.t_business_metric_recomputations'::regclass,
        'public.t_entity_capability_contracts'::regclass,
        'public.t_business_metric_audit'::regclass,
        'public.t_business_metric_acceptance_reports'::regclass
      );

    SELECT count(*), count(*) FILTER (WHERE EXISTS (
      SELECT 1
      FROM pg_catalog.pg_constraint AS constraint_record
      WHERE constraint_record.conrelid = to_regclass(
              'public.' || required.table_name
            )
        AND constraint_record.conname = required.constraint_name
        AND constraint_record.contype = required.constraint_type::"char"
    )) INTO schema_043_expected_constraints, schema_043_constraints
    FROM (VALUES
      ('t_business_metric_installation_plans', 'uq_business_metric_plan_digest', 'u'),
      ('t_business_metric_installation_plans', 'fk_business_metric_plan_previous_installation', 'f'),
      ('t_installed_business_metrics', 'chk_business_metric_installation_lineage_shape', 'c'),
      ('t_installed_business_metrics', 'fk_business_metric_installation_previous', 'f'),
      ('t_installed_business_metrics', 'uq_business_metric_installation_revision', 'u'),
      ('t_installed_business_metrics', 'uq_business_metric_installation_entity', 'u'),
      ('t_installed_business_metrics', 'uq_business_metric_installation_plan', 'u'),
      ('t_installed_business_metrics', 'uq_business_metric_installation_idempotency', 'u'),
      ('t_business_metric_source_bindings', 'uq_business_metric_source_binding_entity', 'u'),
      ('t_business_metric_source_bindings', 'chk_business_metric_source_binding_counter', 'c'),
      ('t_l2_observations', 'chk_l2_typed_value', 'c'),
      ('t_l2_latest', 'chk_l2_latest_typed_value', 'c'),
      ('t_l2_observations', 'chk_l2_observation_event_time_basis', 'c'),
      ('t_l2_latest', 'chk_l2_latest_event_time_basis', 'c'),
      ('t_l2_observation_sources', 'chk_l2_source_event_time_basis', 'c'),
      ('t_business_metric_window_results', 'chk_business_metric_window_method', 'c'),
      ('t_business_metric_window_results', 'chk_business_metric_window_source_count', 'c'),
      ('t_business_metric_window_results', 'chk_business_metric_window_formal_sources', 'c'),
      ('t_business_metric_window_results', 'chk_business_metric_window_source_order', 'c'),
      ('t_business_metric_window_results', 'chk_business_metric_window_source_range', 'c'),
      ('t_business_metric_window_results', 'fk_business_metric_window_first_source', 'f'),
      ('t_business_metric_window_results', 'fk_business_metric_window_last_source', 'f'),
      ('t_business_metric_window_results', 'fk_business_metric_window_result', 'f'),
      ('t_business_metric_window_results', 'fk_business_metric_window_result_installation', 'f'),
      ('t_business_metric_recomputations', 'uq_business_metric_recomputation_revision', 'u'),
      ('t_entity_capability_contracts', 'uq_business_metric_capability_installation_digest', 'u'),
      ('t_business_metric_audit', 'fk_business_metric_audit_installation_plan', 'f'),
      ('t_business_metric_audit', 'chk_business_metric_audit_lifecycle', 'c'),
      ('t_business_metric_acceptance_reports', 'fk_business_metric_acceptance_window_result', 'f'),
      ('t_business_metric_acceptance_reports', 'chk_business_metric_acceptance_installation', 'c'),
      ('t_business_metric_acceptance_reports', 'uq_business_metric_acceptance_installation_digest', 'u')
    ) AS required(table_name, constraint_name, constraint_type);

    SELECT count(*), count(*) FILTER (WHERE EXISTS (
      SELECT 1
      FROM pg_catalog.pg_constraint AS constraint_record
      WHERE constraint_record.conrelid = to_regclass(
              'public.' || required.table_name
            )
        AND constraint_record.conname = required.constraint_name
        AND constraint_record.contype = required.constraint_type::"char"
        AND regexp_replace(
              CASE
                WHEN required.constraint_type = 'c' THEN
                  pg_get_expr(constraint_record.conbin, constraint_record.conrelid)
                ELSE pg_get_constraintdef(constraint_record.oid)
              END,
              '[[:space:]]+', ' ', 'g'
            ) = required.definition
    )) INTO
      schema_043_expected_constraint_definitions,
      schema_043_constraint_definitions
    FROM (VALUES
      (
        't_l2_observations',
        'chk_l2_typed_value', 'c',
        '(((quality = ANY (ARRAY[0, 1])) AND (num_nonnulls(value_float, value_int, value_numeric, value_bool, value_text, value_codes) = 0)) OR ((quality = ANY (ARRAY[64, 192])) AND (num_nonnulls(value_float, value_int, value_numeric, value_bool, value_text, value_codes) = 1)))'
      ),
      (
        't_l2_latest',
        'chk_l2_latest_typed_value', 'c',
        '(((quality = ANY (ARRAY[0, 1])) AND (num_nonnulls(value_float, value_int, value_numeric, value_bool, value_text, value_codes) = 0)) OR ((quality = ANY (ARRAY[64, 192])) AND (num_nonnulls(value_float, value_int, value_numeric, value_bool, value_text, value_codes) = 1)))'
      ),
      (
        't_l2_observations',
        'chk_l2_observation_event_time_basis', 'c',
        '(event_time_basis = ANY (ARRAY[''unknown''::text, ''observed_at''::text, ''received_at''::text, ''calculated_at''::text]))'
      ),
      (
        't_l2_latest',
        'chk_l2_latest_event_time_basis', 'c',
        '(event_time_basis = ANY (ARRAY[''unknown''::text, ''observed_at''::text, ''received_at''::text, ''calculated_at''::text]))'
      ),
      (
        't_l2_observation_sources',
        'chk_l2_source_event_time_basis', 'c',
        '(source_event_time_basis = ANY (ARRAY[''unknown''::text, ''observed_at''::text, ''received_at''::text, ''calculated_at''::text]))'
      ),
      (
        't_business_metric_source_bindings',
        'chk_business_metric_source_binding_counter', 'c',
        '(((method <> ''counter_delta''::text) AND (counter_maximum IS NULL) AND (counter_bit_width IS NULL) AND (counter_reset_on_decrease IS NULL) AND (counter_rollover_on_decrease IS NULL)) OR ((method = ''counter_delta''::text) AND ((counter_maximum IS NOT NULL) AND (counter_maximum >= (0)::numeric) AND (counter_bit_width = ANY (ARRAY[16, 32, 64])) AND (counter_maximum = CASE counter_bit_width WHEN 16 THEN (65535)::numeric WHEN 32 THEN (''4294967295''::bigint)::numeric WHEN 64 THEN ''18446744073709551615''::numeric ELSE NULL::numeric END) AND (counter_reset_on_decrease IS NOT NULL) AND (counter_rollover_on_decrease IS NOT NULL) AND (NOT (counter_reset_on_decrease AND counter_rollover_on_decrease)))))'
      ),
      (
        't_business_metric_installation_plans',
        'fk_business_metric_plan_previous_installation', 'f',
        'FOREIGN KEY (previous_installation_id) REFERENCES t_installed_business_metrics(id)'
      ),
      (
        't_installed_business_metrics',
        'chk_business_metric_installation_lineage_shape', 'c',
        '(((installation_revision = 1) AND (previous_installation_id IS NULL)) OR ((installation_revision > 1) AND (previous_installation_id IS NOT NULL)))'
      ),
      (
        't_business_metric_window_results',
        'chk_business_metric_window_method', 'c',
        '(calculation_method = ANY (ARRAY[''counter_delta''::text, ''power_integral''::text, ''average''::text, ''maximum''::text]))'
      ),
      (
        't_business_metric_window_results',
        'chk_business_metric_window_source_count', 'c',
        '(((source_count = 0) AND (first_source_event_id IS NULL) AND (first_source_observed_at IS NULL) AND (first_source_effective_at IS NULL) AND (last_source_event_id IS NULL) AND (last_source_observed_at IS NULL) AND (last_source_effective_at IS NULL)) OR ((source_count > 0) AND (first_source_event_id IS NOT NULL) AND (first_source_observed_at IS NOT NULL) AND (first_source_effective_at IS NOT NULL) AND (last_source_event_id IS NOT NULL) AND (last_source_observed_at IS NOT NULL) AND (last_source_effective_at IS NOT NULL)))'
      ),
      (
        't_business_metric_window_results',
        'chk_business_metric_window_formal_sources', 'c',
        '((lifecycle = ''invalid''::text) OR (source_count > 0))'
      ),
      (
        't_business_metric_window_results',
        'chk_business_metric_window_source_order', 'c',
        '((source_count = 0) OR ((source_count = 1) AND (NOT (first_source_event_id IS DISTINCT FROM last_source_event_id)) AND (NOT (first_source_effective_at IS DISTINCT FROM last_source_effective_at))) OR ((source_count > 1) AND (ROW(first_source_effective_at, first_source_event_id) < ROW(last_source_effective_at, last_source_event_id))))'
      ),
      (
        't_business_metric_window_results',
        'chk_business_metric_window_source_range', 'c',
        '((source_count = 0) OR ((last_source_effective_at >= window_started_at) AND (last_source_effective_at <= window_ended_at) AND (first_source_effective_at <= window_ended_at) AND (((calculation_method = ''counter_delta''::text) AND (first_source_effective_at >= (window_started_at - (window_ended_at - window_started_at)))) OR ((calculation_method <> ''counter_delta''::text) AND (first_source_effective_at >= window_started_at)))))'
      ),
      (
        't_business_metric_window_results',
        'fk_business_metric_window_result_installation', 'f',
        'FOREIGN KEY (installed_metric_id, result_entity_instance_id) REFERENCES t_installed_business_metrics(id, entity_instance_id)'
      ),
      (
        't_business_metric_audit',
        'fk_business_metric_audit_installation_plan', 'f',
        'FOREIGN KEY (installed_metric_id, plan_id) REFERENCES t_installed_business_metrics(id, source_plan_id)'
      ),
      (
        't_business_metric_audit',
        'chk_business_metric_audit_lifecycle', 'c',
        '(((action = ''disabled''::text) AND (installed_metric_id IS NOT NULL) AND (plan_id IS NOT NULL) AND (resulting_state IS NOT NULL) AND (resulting_state = ''disabled''::text)) OR ((action = ANY (ARRAY[''installed''::text, ''upgraded''::text, ''reused''::text, ''enabled''::text])) AND (installed_metric_id IS NOT NULL) AND (plan_id IS NOT NULL) AND (resulting_state IS NOT NULL) AND (resulting_state = ''active''::text)) OR ((action = ''recomputed''::text) AND (installed_metric_id IS NOT NULL) AND (resulting_state IS NULL)) OR ((action = ''rejected''::text) AND (resulting_state IS NULL)))'
      ),
      (
        't_business_metric_acceptance_reports',
        'fk_business_metric_acceptance_window_result', 'f',
        'FOREIGN KEY (window_result_installed_metric_id, window_result_started_at, window_result_ended_at, window_result_revision) REFERENCES t_business_metric_window_results(installed_metric_id, window_started_at, window_ended_at, revision)'
      ),
      (
        't_business_metric_acceptance_reports',
        'chk_business_metric_acceptance_installation', 'c',
        '(window_result_installed_metric_id = installed_metric_id)'
      )
    ) AS required(table_name, constraint_name, constraint_type, definition);

    SELECT count(*) INTO schema_043_immutable_triggers
    FROM pg_catalog.pg_trigger AS trigger
    JOIN pg_catalog.pg_class AS relation ON relation.oid = trigger.tgrelid
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE NOT trigger.tgisinternal
      AND namespace.nspname = 'public'
      AND relation.relname IN (
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
      )
      AND trigger.tgenabled = 'O'
      AND trigger.tgqual IS NULL
      AND trigger.tgfoid =
            'public.reject_data_trunk_append_only()'::regprocedure
      AND (
        (
          trigger.tgname = 'trg_' || relation.relname || '_immutable'
          AND trigger.tgtype = 27
        )
        OR (
          trigger.tgname = 'trg_' || relation.relname || '_no_truncate'
          AND trigger.tgtype = 34
        )
      );
    SELECT count(*), count(*) FILTER (WHERE EXISTS (
      SELECT 1
      FROM pg_catalog.pg_trigger AS trigger
      WHERE trigger.tgrelid = to_regclass(
              'public.' || required.table_name
            )
        AND trigger.tgname = required.trigger_name
        AND NOT trigger.tgisinternal
        AND trigger.tgenabled = 'O'
        AND trigger.tgqual IS NULL
        AND trigger.tgfoid = to_regprocedure(
              'public.' || required.function_name
            )
        AND trigger.tgtype = required.trigger_type
    )) INTO schema_043_expected_contract_triggers, schema_043_contract_triggers
    FROM (VALUES
      ('t_installed_business_metrics',
       'trg_business_metric_installation_lineage',
       'validate_business_metric_installation_lineage()', 7),
      ('t_business_metric_projections',
       'trg_business_metric_projection_guard',
       'guard_business_metric_projection()', 31),
      ('t_business_metric_projections',
       'trg_business_metric_projection_no_truncate',
       'guard_business_metric_projection()', 34),
      ('t_business_metric_window_results',
       'trg_business_metric_window_result_evidence',
       'validate_business_metric_window_result_evidence()', 7),
      ('t_business_metric_acceptance_reports',
       'trg_business_metric_acceptance_runtime',
       'validate_business_metric_acceptance_runtime()', 7)
    ) AS required(table_name, trigger_name, function_name, trigger_type);

    SELECT count(*), count(*) FILTER (WHERE EXISTS (
      SELECT 1
      FROM pg_catalog.pg_proc AS procedure
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = procedure.pronamespace
      JOIN pg_catalog.pg_language AS language
        ON language.oid = procedure.prolang
      WHERE namespace.nspname = 'public'
        AND procedure.proname = required.function_name
        AND procedure.pronargs = 0
        AND procedure.prokind = 'f'
        AND procedure.prorettype = 'pg_catalog.trigger'::regtype
        AND language.lanname = 'plpgsql'
        AND procedure.provolatile = 'v'
        AND procedure.proparallel = 'u'
        AND NOT procedure.proisstrict
        AND NOT procedure.prosecdef
        AND NOT procedure.proleakproof
        AND procedure.proconfig =
              ARRAY['search_path=pg_catalog, public']::TEXT[]
        AND md5(regexp_replace(
              pg_get_functiondef(procedure.oid), '[[:space:]]+', ' ', 'g'
            )) = required.definition_digest
    )) INTO
      schema_043_expected_function_contracts,
      schema_043_function_contracts
    FROM (VALUES
      ('guard_business_metric_projection',
       'a1a095a52b113800fe640f71cd30e7d4'),
      ('reject_data_trunk_append_only',
       '055c32ce480d817fe7a82c47d42214bf'),
      ('validate_business_metric_acceptance_runtime',
       '865b7220420db3a191c510ca317b0e15'),
      ('validate_business_metric_installation_lineage',
       '200dd4022c853d06e98348abbaea0739'),
      ('validate_business_metric_window_result_evidence',
       '86f6b3a86854d7b1dd7035661b5aca0b')
    ) AS required(function_name, definition_digest);

    IF existing_contract_columns <> schema_043_expected_columns
       OR schema_043_primary_keys <> 12
       OR schema_043_constraints <> schema_043_expected_constraints
       OR schema_043_constraint_definitions
          <> schema_043_expected_constraint_definitions
       OR schema_043_immutable_triggers <> 22
       OR schema_043_contract_triggers <> schema_043_expected_contract_triggers
       OR schema_043_function_contracts
          <> schema_043_expected_function_contracts
       OR NOT EXISTS (
         SELECT 1 FROM pg_index
         WHERE indexrelid = to_regclass('public.uq_business_metric_installation_successor')
           AND indisunique
       )
       OR NOT EXISTS (
         SELECT 1 FROM pg_index
         WHERE indexrelid = to_regclass('public.uq_business_metric_audit_idempotency')
           AND indisunique
       )
       OR NOT EXISTS (
         SELECT 1 FROM pg_index
         WHERE indexrelid = to_regclass('public.uq_l2_event_observed_entity')
           AND indisunique
       )
       OR to_regclass('public.t_l2_observation_commit_sequence_seq') IS NULL
       OR NOT EXISTS (
         SELECT 1 FROM pg_index
         WHERE indexrelid = to_regclass('public.ix_l2_observation_commit_sequence')
           AND NOT indisunique
       )
       OR NOT EXISTS (
         SELECT 1
         FROM pg_attrdef AS default_record
         JOIN pg_attribute AS attribute
           ON attribute.attrelid = default_record.adrelid
          AND attribute.attnum = default_record.adnum
         WHERE default_record.adrelid = to_regclass('public.t_l2_observations')
           AND attribute.attname = 'commit_sequence'
           AND pg_get_expr(default_record.adbin, default_record.adrelid)
                 LIKE 'nextval(%t_l2_observation_commit_sequence_seq%'
       ) THEN
      RAISE EXCEPTION 'SCHEMA_043_PARTIAL_STRUCTURE: schema 043 is malformed'
        USING ERRCODE = '55000';
    END IF;
  END IF;
END;
$$;

-- A physical/logical node has one ordinary current point-processing program,
-- while each installed business metric owns an independent private program.
-- Keeping that distinction on the shared installation record lets Task 2
-- reuse the existing atomic apply path without one metric superseding another.
DO $$
BEGIN
  -- A replay has already been verified above and must not repair or rewrite
  -- this shared point-processing contract.  Only a pristine 042 upgrade may
  -- create the Schema 043 extension.
  IF to_regclass('public.t_business_metric_templates') IS NULL THEN
    ALTER TABLE t_installed_point_processings
      ADD COLUMN processing_scope TEXT NOT NULL DEFAULT 'node';
    ALTER TABLE t_installed_point_processings
      ADD COLUMN processing_owner_key UUID;
    ALTER TABLE t_point_processing_revisions
      ADD COLUMN internal_kind TEXT;
    ALTER TABLE t_point_processing_revisions
      ADD CONSTRAINT chk_point_processing_revision_internal_kind
      CHECK (internal_kind IS NULL OR internal_kind = 'business_metric');
    ALTER TABLE t_installed_point_processings
      ADD CONSTRAINT chk_installed_point_processing_scope
      CHECK (
        (processing_scope = 'node' AND processing_owner_key IS NULL)
        OR (
          processing_scope = 'business_metric'
          AND processing_owner_key IS NOT NULL
        )
      );

    DROP INDEX IF EXISTS uq_installed_point_conversion_current;
    DROP INDEX IF EXISTS uq_installed_point_processing_current;
    CREATE UNIQUE INDEX uq_installed_point_processing_current
      ON t_installed_point_processings(node_id)
      WHERE current = TRUE AND processing_scope = 'node';
    CREATE UNIQUE INDEX uq_installed_business_metric_processing_current
      ON t_installed_point_processings(node_id, processing_owner_key)
      WHERE current = TRUE AND processing_scope = 'business_metric';
  END IF;
END;
$$;

ALTER TABLE public.t_l2_observations
  ADD COLUMN IF NOT EXISTS event_time_basis TEXT NOT NULL DEFAULT 'received_at';
ALTER TABLE public.t_l2_observations
  ADD COLUMN IF NOT EXISTS value_numeric NUMERIC;
ALTER TABLE public.t_l2_observations
  DROP CONSTRAINT IF EXISTS chk_l2_typed_value,
  ADD CONSTRAINT chk_l2_typed_value CHECK (
    (quality IN (0,1) AND num_nonnulls(
      value_float, value_int, value_numeric, value_bool, value_text, value_codes
    ) = 0)
    OR
    (quality IN (64,192) AND num_nonnulls(
      value_float, value_int, value_numeric, value_bool, value_text, value_codes
    ) = 1)
  );
CREATE SEQUENCE IF NOT EXISTS public.t_l2_observation_commit_sequence_seq AS BIGINT;
ALTER TABLE public.t_l2_observations
  ADD COLUMN IF NOT EXISTS commit_sequence BIGINT NOT NULL DEFAULT
    nextval('public.t_l2_observation_commit_sequence_seq');
SELECT setval(
  'public.t_l2_observation_commit_sequence_seq',
  GREATEST(COALESCE((SELECT max(commit_sequence) FROM public.t_l2_observations), 0), 1),
  EXISTS (SELECT 1 FROM public.t_l2_observations)
);
ALTER SEQUENCE public.t_l2_observation_commit_sequence_seq
  OWNED BY public.t_l2_observations.commit_sequence;
ALTER TABLE public.t_l2_observations
  ALTER COLUMN commit_sequence SET DEFAULT
    nextval('public.t_l2_observation_commit_sequence_seq'),
  ALTER COLUMN commit_sequence SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_l2_observation_commit_sequence
  ON public.t_l2_observations(commit_sequence);
ALTER TABLE public.t_l2_observations
  DROP CONSTRAINT IF EXISTS chk_l2_observation_event_time_basis,
  ADD CONSTRAINT chk_l2_observation_event_time_basis
    CHECK (event_time_basis IN ('unknown','observed_at','received_at','calculated_at'));

ALTER TABLE public.t_l2_latest
  ADD COLUMN IF NOT EXISTS event_time_basis TEXT NOT NULL DEFAULT 'received_at';
ALTER TABLE public.t_l2_latest
  ADD COLUMN IF NOT EXISTS value_numeric NUMERIC;
ALTER TABLE public.t_l2_latest
  DROP CONSTRAINT IF EXISTS chk_l2_latest_typed_value,
  ADD CONSTRAINT chk_l2_latest_typed_value CHECK (
    (quality IN (0,1) AND num_nonnulls(
      value_float, value_int, value_numeric, value_bool, value_text, value_codes
    ) = 0)
    OR
    (quality IN (64,192) AND num_nonnulls(
      value_float, value_int, value_numeric, value_bool, value_text, value_codes
    ) = 1)
  );

CREATE OR REPLACE FUNCTION public.validate_l2_typed_value_against_entity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  expected_type TEXT;
  value_count INTEGER;
  value_matches BOOLEAN;
BEGIN
  SELECT data_type INTO expected_type
  FROM public.t_entity_instances
  WHERE id = NEW.entity_instance_id;
  IF expected_type IS NULL THEN
    RETURN NEW;
  END IF;
  value_count := num_nonnulls(
    NEW.value_float, NEW.value_int, NEW.value_numeric,
    NEW.value_bool, NEW.value_text, NEW.value_codes
  );
  IF NEW.quality IN (0,1) THEN
    value_matches := value_count = 0;
  ELSE
    value_matches := value_count = 1 AND CASE expected_type
      WHEN 'FLOAT' THEN NEW.value_float IS NOT NULL OR NEW.value_numeric IS NOT NULL
      WHEN 'INT' THEN NEW.value_int IS NOT NULL OR NEW.value_numeric IS NOT NULL
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
ALTER TABLE public.t_l2_latest
  DROP CONSTRAINT IF EXISTS chk_l2_latest_event_time_basis,
  ADD CONSTRAINT chk_l2_latest_event_time_basis
    CHECK (event_time_basis IN ('unknown','observed_at','received_at','calculated_at'));

ALTER TABLE public.t_l2_observation_sources
  ADD COLUMN IF NOT EXISTS source_event_time_basis TEXT NOT NULL DEFAULT 'received_at';
ALTER TABLE public.t_l2_observation_sources
  DROP CONSTRAINT IF EXISTS chk_l2_source_event_time_basis,
  ADD CONSTRAINT chk_l2_source_event_time_basis
    CHECK (source_event_time_basis IN ('unknown','observed_at','received_at','calculated_at'));

ALTER TABLE public.t_tags
  ADD COLUMN IF NOT EXISTS timestamp_trusted BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.t_telemetry
  ADD COLUMN IF NOT EXISTS event_time_basis TEXT NOT NULL DEFAULT 'received_at';
ALTER TABLE public.t_telemetry
  ADD COLUMN IF NOT EXISTS event_received_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE public.t_telemetry_latest
  ADD COLUMN IF NOT EXISTS event_time_basis TEXT NOT NULL DEFAULT 'received_at';
ALTER TABLE public.t_telemetry_latest
  ADD COLUMN IF NOT EXISTS event_received_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE public.t_telemetry
  DROP CONSTRAINT IF EXISTS chk_l0_event_time_basis,
  ADD CONSTRAINT chk_l0_event_time_basis
    CHECK (event_time_basis IN ('unknown','observed_at','received_at'));
ALTER TABLE public.t_telemetry_latest
  DROP CONSTRAINT IF EXISTS chk_l0_latest_event_time_basis,
  ADD CONSTRAINT chk_l0_latest_event_time_basis
    CHECK (event_time_basis IN ('unknown','observed_at','received_at'));

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
  digest CHAR(64) NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
  planned_by TEXT NOT NULL CHECK (btrim(planned_by) <> ''),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_business_metric_plan_digest UNIQUE(digest),
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
  installation_revision INTEGER NOT NULL DEFAULT 1
    CHECK (installation_revision > 0),
  previous_installation_id UUID,
  installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fk_business_metric_installation_previous
    FOREIGN KEY(previous_installation_id)
    REFERENCES t_installed_business_metrics(id),
  CONSTRAINT chk_business_metric_installation_lineage_shape CHECK (
    (installation_revision = 1 AND previous_installation_id IS NULL)
    OR (installation_revision > 1 AND previous_installation_id IS NOT NULL)
  ),
  CONSTRAINT uq_business_metric_installation_revision
    UNIQUE(node_id, entity_instance_id, installation_revision),
  CONSTRAINT uq_business_metric_installation_entity
    UNIQUE(id, entity_instance_id),
  CONSTRAINT uq_business_metric_installation_plan
    UNIQUE(id, source_plan_id),
  CONSTRAINT uq_business_metric_installation_idempotency
    UNIQUE(installed_by, idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_business_metric_installation_successor
  ON t_installed_business_metrics(previous_installation_id)
  WHERE previous_installation_id IS NOT NULL;

CREATE OR REPLACE FUNCTION public.validate_business_metric_installation_lineage()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
  previous_row public.t_installed_business_metrics%ROWTYPE;
BEGIN
  IF NEW.previous_installation_id IS NULL THEN
    IF NEW.installation_revision <> 1 THEN
      RAISE EXCEPTION 'first business metric installation revision must be 1'
        USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
  END IF;

  SELECT * INTO previous_row
  FROM public.t_installed_business_metrics
  WHERE id = NEW.previous_installation_id;
  IF NOT FOUND
     OR previous_row.node_id <> NEW.node_id
     OR previous_row.entity_instance_id <> NEW.entity_instance_id
     OR NEW.installation_revision <> previous_row.installation_revision + 1 THEN
    RAISE EXCEPTION 'business metric installation lineage is invalid'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_business_metric_installation_lineage
  ON public.t_installed_business_metrics;
CREATE TRIGGER trg_business_metric_installation_lineage
BEFORE INSERT ON public.t_installed_business_metrics
FOR EACH ROW EXECUTE FUNCTION public.validate_business_metric_installation_lineage();

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
  maximum_sample_gap_seconds INTEGER NOT NULL
    CHECK (maximum_sample_gap_seconds > 0),
  producer_contract_digest CHAR(64) NOT NULL
    CHECK (producer_contract_digest ~ '^[0-9a-f]{64}$'),
  counter_maximum NUMERIC,
  counter_bit_width SMALLINT,
  counter_reset_on_decrease BOOLEAN,
  counter_rollover_on_decrease BOOLEAN,
  source_digest CHAR(64) NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
  PRIMARY KEY(installed_metric_id, ordinal),
  CONSTRAINT uq_business_metric_source_binding_entity
    UNIQUE(installed_metric_id, entity_instance_id),
  CONSTRAINT chk_business_metric_source_binding_counter CHECK (
    (
      method <> 'counter_delta'
      AND counter_maximum IS NULL
      AND counter_bit_width IS NULL
      AND counter_reset_on_decrease IS NULL
      AND counter_rollover_on_decrease IS NULL
    )
    OR (
      method = 'counter_delta'
      AND (
          counter_maximum IS NOT NULL
          AND counter_maximum >= 0
          AND counter_bit_width IN (16,32,64)
          AND counter_maximum = CASE counter_bit_width
              WHEN 16 THEN 65535::NUMERIC
              WHEN 32 THEN 4294967295::NUMERIC
              WHEN 64 THEN 18446744073709551615::NUMERIC
            END
          AND counter_reset_on_decrease IS NOT NULL
          AND counter_rollover_on_decrease IS NOT NULL
          AND NOT (counter_reset_on_decrease AND counter_rollover_on_decrease)
      )
    )
  )
);

CREATE TABLE IF NOT EXISTS t_business_metric_projections (
  installed_metric_id UUID PRIMARY KEY REFERENCES t_installed_business_metrics(id),
  window_started_at TIMESTAMPTZ NOT NULL,
  window_ended_at TIMESTAMPTZ NOT NULL CHECK (window_ended_at > window_started_at),
  watermark_at TIMESTAMPTZ,
  coverage DOUBLE PRECISION NOT NULL CHECK (coverage >= 0 AND coverage <= 1),
  quality SMALLINT NOT NULL CHECK (quality IN (0,64,192)),
  estimated BOOLEAN NOT NULL,
  last_commit_sequence BIGINT NOT NULL DEFAULT 0 CHECK (last_commit_sequence >= 0),
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
    CONSTRAINT chk_business_metric_window_method
    CHECK (calculation_method IN ('counter_delta','power_integral','average','maximum')),
  quality SMALLINT NOT NULL CHECK (quality IN (0,64,192)),
  coverage DOUBLE PRECISION NOT NULL CHECK (coverage >= 0 AND coverage <= 1),
  estimated BOOLEAN NOT NULL,
  source_count INTEGER NOT NULL CHECK (source_count >= 0),
  first_source_event_id UUID,
  first_source_observed_at TIMESTAMPTZ,
  first_source_effective_at TIMESTAMPTZ,
  last_source_event_id UUID,
  last_source_observed_at TIMESTAMPTZ,
  last_source_effective_at TIMESTAMPTZ,
  result_event_id UUID,
  result_observed_at TIMESTAMPTZ,
  result_entity_instance_id UUID,
  content_digest CHAR(64) NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
  source_summary JSONB NOT NULL CHECK (jsonb_typeof(source_summary) = 'object'),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(installed_metric_id, window_started_at, window_ended_at, revision),
  CONSTRAINT fk_business_metric_window_first_source
    FOREIGN KEY(first_source_event_id, first_source_observed_at)
    REFERENCES t_l2_observations(event_id, observed_at),
  CONSTRAINT fk_business_metric_window_last_source
    FOREIGN KEY(last_source_event_id, last_source_observed_at)
    REFERENCES t_l2_observations(event_id, observed_at),
  CONSTRAINT fk_business_metric_window_result
    FOREIGN KEY(result_event_id, result_observed_at, result_entity_instance_id)
    REFERENCES t_l2_observations(event_id, observed_at, entity_instance_id),
  CONSTRAINT fk_business_metric_window_result_installation
    FOREIGN KEY(installed_metric_id, result_entity_instance_id)
    REFERENCES t_installed_business_metrics(id, entity_instance_id),
  CHECK (
    (first_source_event_id IS NULL)
      = (first_source_observed_at IS NULL AND first_source_effective_at IS NULL)
  ),
  CHECK (
    (last_source_event_id IS NULL)
      = (last_source_observed_at IS NULL AND last_source_effective_at IS NULL)
  ),
  CONSTRAINT chk_business_metric_window_source_count CHECK (
    (source_count = 0
      AND first_source_event_id IS NULL AND first_source_observed_at IS NULL
      AND first_source_effective_at IS NULL
      AND last_source_event_id IS NULL AND last_source_observed_at IS NULL
      AND last_source_effective_at IS NULL)
    OR
    (source_count > 0
      AND first_source_event_id IS NOT NULL AND first_source_observed_at IS NOT NULL
      AND first_source_effective_at IS NOT NULL
      AND last_source_event_id IS NOT NULL AND last_source_observed_at IS NOT NULL
      AND last_source_effective_at IS NOT NULL)
  ),
  CONSTRAINT chk_business_metric_window_formal_sources CHECK (
    lifecycle = 'invalid' OR source_count > 0
  ),
  CONSTRAINT chk_business_metric_window_source_order CHECK (
    source_count = 0
    OR (
      source_count = 1
      AND first_source_event_id IS NOT DISTINCT FROM last_source_event_id
      AND first_source_effective_at IS NOT DISTINCT FROM last_source_effective_at
    )
    OR (
      source_count > 1
      AND ROW(first_source_effective_at, first_source_event_id)
          < ROW(last_source_effective_at, last_source_event_id)
    )
  ),
  -- Counter delta may use one baseline event before the aligned window.  Bound
  -- that look-back to one window duration; all other aggregators must draw
  -- their first and last evidence from the closed [window_start, window_end].
  CONSTRAINT chk_business_metric_window_source_range CHECK (
    source_count = 0
    OR (
      last_source_effective_at >= window_started_at
      AND last_source_effective_at <= window_ended_at
      AND first_source_effective_at <= window_ended_at
      AND (
        (
          calculation_method = 'counter_delta'
          AND first_source_effective_at
              >= window_started_at - (window_ended_at - window_started_at)
        )
        OR (
          calculation_method <> 'counter_delta'
          AND first_source_effective_at >= window_started_at
        )
      )
    )
  ),
  CHECK (
    (lifecycle IN ('completed','corrected')
      AND result_event_id IS NOT NULL AND result_observed_at IS NOT NULL
      AND result_entity_instance_id IS NOT NULL)
    OR (lifecycle = 'invalid' AND result_event_id IS NULL
      AND result_observed_at IS NULL AND result_entity_instance_id IS NULL)
  )
);

CREATE OR REPLACE FUNCTION public.validate_business_metric_window_result_evidence()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
  IF NOT EXISTS (
       SELECT 1
       FROM public.t_business_metric_source_bindings AS binding
       WHERE binding.installed_metric_id = NEW.installed_metric_id
         AND binding.method = NEW.calculation_method
     )
     OR EXISTS (
       SELECT 1
       FROM public.t_business_metric_source_bindings AS binding
       WHERE binding.installed_metric_id = NEW.installed_metric_id
         AND binding.method <> NEW.calculation_method
     ) THEN
    RAISE EXCEPTION 'window result method does not match frozen sources'
      USING ERRCODE = '23514';
  END IF;

  IF NEW.source_count > 0 THEN
    IF NEW.first_source_effective_at > NEW.last_source_effective_at
       OR (
         NEW.source_count = 1
         AND (
           NEW.first_source_event_id <> NEW.last_source_event_id
           OR NEW.first_source_effective_at <> NEW.last_source_effective_at
         )
       ) THEN
      RAISE EXCEPTION 'window result source event range is inconsistent'
        USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (
         SELECT 1
         FROM public.t_l2_observations AS observation
         JOIN public.t_business_metric_source_bindings AS binding
           ON binding.installed_metric_id = NEW.installed_metric_id
          AND binding.entity_instance_id = observation.entity_instance_id
         WHERE observation.event_id = NEW.first_source_event_id
           AND observation.observed_at = NEW.first_source_observed_at
           AND NEW.first_source_effective_at IS NOT DISTINCT FROM CASE
                 observation.event_time_basis
                 WHEN 'observed_at' THEN observation.observed_at
                 WHEN 'calculated_at' THEN observation.calculated_at
                 ELSE observation.received_at
               END
       )
       OR NOT EXISTS (
         SELECT 1
         FROM public.t_l2_observations AS observation
         JOIN public.t_business_metric_source_bindings AS binding
           ON binding.installed_metric_id = NEW.installed_metric_id
          AND binding.entity_instance_id = observation.entity_instance_id
         WHERE observation.event_id = NEW.last_source_event_id
           AND observation.observed_at = NEW.last_source_observed_at
           AND NEW.last_source_effective_at IS NOT DISTINCT FROM CASE
                 observation.event_time_basis
                 WHEN 'observed_at' THEN observation.observed_at
                 WHEN 'calculated_at' THEN observation.calculated_at
                 ELSE observation.received_at
               END
       ) THEN
      RAISE EXCEPTION 'window result source events are not frozen sources'
        USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_business_metric_window_result_evidence
  ON public.t_business_metric_window_results;
CREATE TRIGGER trg_business_metric_window_result_evidence
BEFORE INSERT ON public.t_business_metric_window_results
FOR EACH ROW EXECUTE FUNCTION public.validate_business_metric_window_result_evidence();

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
  CONSTRAINT uq_business_metric_recomputation_revision
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
  CONSTRAINT uq_business_metric_capability_installation_digest
    UNIQUE(installed_metric_id, digest)
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
  CONSTRAINT fk_business_metric_audit_installation_plan
    FOREIGN KEY(installed_metric_id, plan_id)
    REFERENCES t_installed_business_metrics(id, source_plan_id),
  CONSTRAINT chk_business_metric_audit_lifecycle CHECK (
    (
      action = 'disabled'
      AND installed_metric_id IS NOT NULL AND plan_id IS NOT NULL
      AND resulting_state IS NOT NULL AND resulting_state = 'disabled'
    )
    OR (
      action IN ('installed','upgraded','reused','enabled')
      AND installed_metric_id IS NOT NULL AND plan_id IS NOT NULL
      AND resulting_state IS NOT NULL AND resulting_state = 'active'
    )
    OR (
      action = 'recomputed'
      AND installed_metric_id IS NOT NULL
      AND resulting_state IS NULL
    )
    OR (action = 'rejected' AND resulting_state IS NULL)
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
  CONSTRAINT fk_business_metric_acceptance_window_result
    FOREIGN KEY(window_result_installed_metric_id, window_result_started_at, window_result_ended_at, window_result_revision)
    REFERENCES t_business_metric_window_results(installed_metric_id, window_started_at, window_ended_at, revision),
  CONSTRAINT chk_business_metric_acceptance_installation
    CHECK (window_result_installed_metric_id = installed_metric_id),
  CONSTRAINT uq_business_metric_acceptance_installation_digest
    UNIQUE(installed_metric_id, digest)
);

CREATE OR REPLACE FUNCTION public.validate_business_metric_acceptance_runtime()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
  window_result public.t_business_metric_window_results%ROWTYPE;
  producing_runtime_id UUID;
BEGIN
  IF NEW.window_result_installed_metric_id IS NULL
     OR NEW.window_result_started_at IS NULL
     OR NEW.window_result_ended_at IS NULL
     OR NEW.window_result_revision IS NULL
     OR NEW.runtime_instance_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT result.* INTO window_result
  FROM public.t_business_metric_window_results AS result
  WHERE result.installed_metric_id = NEW.window_result_installed_metric_id
    AND result.window_started_at = NEW.window_result_started_at
    AND result.window_ended_at = NEW.window_result_ended_at
    AND result.revision = NEW.window_result_revision;

  IF NOT FOUND
     OR window_result.lifecycle NOT IN ('completed', 'corrected')
     OR window_result.source_count <= 0
     OR window_result.first_source_event_id IS NULL
     OR window_result.first_source_observed_at IS NULL
     OR window_result.first_source_effective_at IS NULL
     OR window_result.last_source_event_id IS NULL
     OR window_result.last_source_observed_at IS NULL
     OR window_result.last_source_effective_at IS NULL
     OR window_result.result_event_id IS NULL
     OR window_result.result_observed_at IS NULL
     OR window_result.result_entity_instance_id IS NULL
     OR (
       window_result.source_count = 1
       AND (
         window_result.first_source_event_id
           IS DISTINCT FROM window_result.last_source_event_id
         OR window_result.first_source_effective_at
           IS DISTINCT FROM window_result.last_source_effective_at
       )
     )
     OR (
       window_result.source_count > 1
       AND NOT (
         ROW(window_result.first_source_effective_at,
             window_result.first_source_event_id)
         < ROW(window_result.last_source_effective_at,
               window_result.last_source_event_id)
       )
     )
     OR window_result.last_source_effective_at
          < window_result.window_started_at
     OR window_result.last_source_effective_at
          > window_result.window_ended_at
     OR window_result.first_source_effective_at
          > window_result.window_ended_at
     OR (
       window_result.calculation_method = 'counter_delta'
       AND window_result.first_source_effective_at
             < window_result.window_started_at
               - (window_result.window_ended_at
                  - window_result.window_started_at)
     )
     OR (
       window_result.calculation_method <> 'counter_delta'
       AND window_result.first_source_effective_at
             < window_result.window_started_at
     ) THEN
    RAISE EXCEPTION 'acceptance result source evidence is invalid'
      USING ERRCODE = '23514';
  END IF;

  IF NOT EXISTS (
       SELECT 1
       FROM public.t_business_metric_source_bindings AS binding
       WHERE binding.installed_metric_id = window_result.installed_metric_id
         AND binding.method = window_result.calculation_method
     )
     OR EXISTS (
       SELECT 1
       FROM public.t_business_metric_source_bindings AS binding
       WHERE binding.installed_metric_id = window_result.installed_metric_id
         AND binding.method <> window_result.calculation_method
     )
     OR NOT EXISTS (
       SELECT 1
       FROM public.t_l2_observations AS observation
       JOIN public.t_business_metric_source_bindings AS binding
         ON binding.installed_metric_id = window_result.installed_metric_id
        AND binding.entity_instance_id = observation.entity_instance_id
       WHERE observation.event_id = window_result.first_source_event_id
         AND observation.observed_at = window_result.first_source_observed_at
         AND window_result.first_source_effective_at IS NOT DISTINCT FROM CASE
               observation.event_time_basis
               WHEN 'observed_at' THEN observation.observed_at
               WHEN 'calculated_at' THEN observation.calculated_at
               ELSE observation.received_at
             END
     )
     OR NOT EXISTS (
       SELECT 1
       FROM public.t_l2_observations AS observation
       JOIN public.t_business_metric_source_bindings AS binding
         ON binding.installed_metric_id = window_result.installed_metric_id
        AND binding.entity_instance_id = observation.entity_instance_id
       WHERE observation.event_id = window_result.last_source_event_id
         AND observation.observed_at = window_result.last_source_observed_at
         AND window_result.last_source_effective_at IS NOT DISTINCT FROM CASE
               observation.event_time_basis
               WHEN 'observed_at' THEN observation.observed_at
               WHEN 'calculated_at' THEN observation.calculated_at
               ELSE observation.received_at
             END
     ) THEN
    RAISE EXCEPTION 'acceptance result does not match frozen sources'
      USING ERRCODE = '23514';
  END IF;

  SELECT observation.producing_runtime_instance_id
  INTO producing_runtime_id
  FROM public.t_l2_observations AS observation
  JOIN public.t_installed_business_metrics AS installation
    ON installation.id = window_result.installed_metric_id
   AND installation.entity_instance_id = observation.entity_instance_id
  WHERE observation.event_id = window_result.result_event_id
    AND observation.observed_at = window_result.result_observed_at
    AND observation.entity_instance_id = window_result.result_entity_instance_id;
  IF producing_runtime_id IS NULL
     OR producing_runtime_id <> NEW.runtime_instance_id THEN
    RAISE EXCEPTION 'acceptance runtime does not match result producer'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_business_metric_acceptance_runtime
  ON public.t_business_metric_acceptance_reports;
CREATE TRIGGER trg_business_metric_acceptance_runtime
BEFORE INSERT ON public.t_business_metric_acceptance_reports
FOR EACH ROW EXECUTE FUNCTION public.validate_business_metric_acceptance_runtime();

CREATE INDEX IF NOT EXISTS ix_business_metric_installed_node ON t_installed_business_metrics(node_id, state);
CREATE INDEX IF NOT EXISTS ix_business_metric_source_bindings_entity ON t_business_metric_source_bindings(entity_instance_id);
CREATE INDEX IF NOT EXISTS ix_business_metric_window_results_lookup ON t_business_metric_window_results(installed_metric_id, window_ended_at DESC, revision DESC);

CREATE OR REPLACE FUNCTION public.guard_business_metric_projection()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
  frozen_timezone TEXT;
  window_contract JSONB;
  window_kind TEXT;
  duration_text TEXT;
  duration_count BIGINT;
  duration_seconds BIGINT;
  expected_start TIMESTAMPTZ;
  expected_end TIMESTAMPTZ;
  frozen_estimated BOOLEAN;
BEGIN
  IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
    RAISE EXCEPTION 'business metric projection rows cannot be deleted or truncated'
      USING ERRCODE = '55000';
  END IF;

  SELECT installed.frozen_timezone, revision.content -> 'window',
         bool_or(binding.estimated)
  INTO frozen_timezone, window_contract, frozen_estimated
  FROM public.t_installed_business_metrics AS installed
  JOIN public.t_business_metric_revisions AS revision
    ON revision.id = installed.template_revision_id
  JOIN public.t_business_metric_source_bindings AS binding
    ON binding.installed_metric_id = installed.id
  WHERE installed.id = NEW.installed_metric_id
  GROUP BY installed.frozen_timezone, revision.content;
  IF NOT FOUND OR frozen_timezone IS NULL OR window_contract IS NULL THEN
    RAISE EXCEPTION 'business metric projection has no frozen window contract'
      USING ERRCODE = '55000';
  END IF;
  window_kind := window_contract ->> 'kind';

  IF NEW.state ->> 'lifecycle' IS DISTINCT FROM 'provisional' THEN
    RAISE EXCEPTION 'business metric current projection must be provisional'
      USING ERRCODE = '55000';
  END IF;

  IF window_kind = 'aligned_daily' THEN
    expected_start := (
      (NEW.window_started_at AT TIME ZONE frozen_timezone)::date::timestamp
      AT TIME ZONE frozen_timezone
    );
    expected_end := (
      ((NEW.window_started_at AT TIME ZONE frozen_timezone)::date + 1)::timestamp
      AT TIME ZONE frozen_timezone
    );
    IF NEW.window_started_at <> expected_start
       OR NEW.window_ended_at <> expected_end THEN
      RAISE EXCEPTION 'business metric daily projection window is invalid'
        USING ERRCODE = '55000';
    END IF;
  ELSIF window_kind = 'rolling' THEN
    duration_text := window_contract ->> 'duration';
    IF duration_text IS NULL OR duration_text !~ '^[0-9]+[smhd]$' THEN
      RAISE EXCEPTION 'business metric rolling duration is invalid'
        USING ERRCODE = '55000';
    END IF;
    duration_count := substring(duration_text FROM '^[0-9]+')::BIGINT;
    duration_seconds := duration_count * CASE right(duration_text, 1)
      WHEN 's' THEN 1 WHEN 'm' THEN 60 WHEN 'h' THEN 3600 WHEN 'd' THEN 86400
    END;
    IF NEW.window_ended_at - NEW.window_started_at
         <> make_interval(secs => duration_seconds)
       OR date_trunc('minute', NEW.window_ended_at) <> NEW.window_ended_at THEN
      RAISE EXCEPTION 'business metric rolling projection window is invalid'
        USING ERRCODE = '55000';
    END IF;
  ELSE
    RAISE EXCEPTION 'business metric projection window kind is invalid'
      USING ERRCODE = '55000';
  END IF;

  IF TG_OP = 'INSERT' THEN
    RETURN NEW;
  END IF;

  IF NEW.installed_metric_id IS DISTINCT FROM OLD.installed_metric_id THEN
    RAISE EXCEPTION 'business metric projection installation is immutable'
      USING ERRCODE = '55000';
  END IF;
  IF (OLD.watermark_at IS NOT NULL AND (
        NEW.watermark_at IS NULL OR NEW.watermark_at < OLD.watermark_at
      )) OR NEW.updated_at < OLD.updated_at
      OR NEW.last_commit_sequence < OLD.last_commit_sequence THEN
    RAISE EXCEPTION 'business metric projection recovery clocks cannot move backwards'
      USING ERRCODE = '55000';
  END IF;
  IF NEW.window_started_at = OLD.window_started_at
     AND NEW.window_ended_at = OLD.window_ended_at THEN
    RETURN NEW;
  END IF;
  IF NEW.state IS NOT DISTINCT FROM OLD.state
     OR NEW.state ->> 'windowStartedAt' IS NULL
     OR NEW.state ->> 'windowEndedAt' IS NULL
     OR (NEW.state ->> 'windowStartedAt')::TIMESTAMPTZ
          IS DISTINCT FROM NEW.window_started_at
     OR (NEW.state ->> 'windowEndedAt')::TIMESTAMPTZ
          IS DISTINCT FROM NEW.window_ended_at THEN
    RAISE EXCEPTION 'business metric projection recovery state must reset for next window'
      USING ERRCODE = '55000';
  END IF;
  IF NEW.coverage <> 0
     OR NEW.quality <> 0
     OR NEW.estimated IS DISTINCT FROM frozen_estimated
     OR NEW.state - ARRAY[
          'lifecycle', 'windowStartedAt', 'windowEndedAt', 'value', 'reason',
          'sourceEventIds', 'sourceSummary', 'peakAt', 'peakEventId'
        ]::TEXT[] <> '{}'::JSONB
     OR NEW.state ->> 'lifecycle' IS DISTINCT FROM 'provisional'
     OR NEW.state -> 'value' IS DISTINCT FROM 'null'::JSONB
     OR NEW.state -> 'reason' IS DISTINCT FROM 'null'::JSONB
     OR NEW.state -> 'sourceEventIds' IS DISTINCT FROM '[]'::JSONB
     OR NEW.state -> 'sourceSummary' IS DISTINCT FROM '{}'::JSONB
     OR NEW.state -> 'peakAt' IS DISTINCT FROM 'null'::JSONB
     OR NEW.state -> 'peakEventId' IS DISTINCT FROM 'null'::JSONB THEN
    RAISE EXCEPTION 'business metric projection next window must use neutral recovery state'
      USING ERRCODE = '55000';
  END IF;
  IF window_kind = 'aligned_daily'
     AND NEW.window_started_at = OLD.window_ended_at THEN
    RETURN NEW;
  END IF;
  IF window_kind = 'rolling'
     AND NEW.window_started_at = OLD.window_started_at + interval '1 minute'
     AND NEW.window_ended_at = OLD.window_ended_at + interval '1 minute' THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'business metric projection window can only advance once'
    USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS trg_business_metric_projection_guard
  ON public.t_business_metric_projections;
CREATE TRIGGER trg_business_metric_projection_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.t_business_metric_projections
FOR EACH ROW EXECUTE FUNCTION public.guard_business_metric_projection();

DROP TRIGGER IF EXISTS trg_business_metric_projection_no_truncate
  ON public.t_business_metric_projections;
CREATE TRIGGER trg_business_metric_projection_no_truncate
BEFORE TRUNCATE ON public.t_business_metric_projections
FOR EACH STATEMENT EXECUTE FUNCTION public.guard_business_metric_projection();

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
  EXECUTE 'ALTER FUNCTION public.reject_data_trunk_append_only() '
          'SET search_path = pg_catalog, public';
  FOREACH table_name IN ARRAY immutable_tables LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON public.%I', 'trg_' || table_name || '_immutable', table_name);
    EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.reject_data_trunk_append_only()', 'trg_' || table_name || '_immutable', table_name);
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON public.%I', 'trg_' || table_name || '_no_truncate', table_name);
    EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON public.%I FOR EACH STATEMENT EXECUTE FUNCTION public.reject_data_trunk_append_only()', 'trg_' || table_name || '_no_truncate', table_name);
  END LOOP;
END;
$$;
