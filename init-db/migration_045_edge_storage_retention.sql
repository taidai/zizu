-- Schema 045: bound edge telemetry storage while preserving source evidence.

BEGIN;

DO $migration$
DECLARE
  required_objects INTEGER;
  history_foreign_keys INTEGER;
  typed_value_checks INTEGER;
  cache_index_valid BOOLEAN;
  prune_procedure_valid BOOLEAN;
  prune_job_valid BOOLEAN;
  telemetry_dimension_valid BOOLEAN;
  telemetry_compression_enabled BOOLEAN;
  storage_jobs_valid BOOLEAN;
  final_contract BOOLEAN;
BEGIN
  SELECT count(*) INTO required_objects
  FROM (VALUES
    ('t_configuration_state'),
    ('t_configuration_revisions'),
    ('t_configuration_audit'),
    ('t_telemetry'),
    ('t_telemetry_latest'),
    ('t_l0_observation_dedup'),
    ('t_l2_observation_sources'),
    ('t_tags'),
    ('tel_agg_5min'),
    ('tel_agg_1h'),
    ('tel_agg_1d')
  ) AS required(name)
  WHERE to_regclass('public.' || required.name) IS NOT NULL;

  IF required_objects <> 11
     OR to_regclass('public.t_solution_packages') IS NOT NULL
     OR NOT EXISTS (
       SELECT 1
       FROM timescaledb_information.hypertables
       WHERE hypertable_schema = 'public'
         AND hypertable_name = 't_telemetry'
     )
     OR (
       SELECT count(*)
       FROM timescaledb_information.continuous_aggregates
       WHERE view_schema = 'public'
         AND view_name IN ('tel_agg_5min', 'tel_agg_1h', 'tel_agg_1d')
     ) <> 3 THEN
    RAISE EXCEPTION
      'SCHEMA_045_PARTIAL_STRUCTURE: complete schema 044 is required'
      USING ERRCODE = '55000';
  END IF;

  SELECT count(*) INTO history_foreign_keys
  FROM pg_constraint
  WHERE contype = 'f'
    AND confrelid = 'public.t_l0_observation_dedup'::regclass
    AND conrelid IN (
      'public.t_telemetry'::regclass,
      'public.t_telemetry_latest'::regclass,
      'public.t_l2_observation_sources'::regclass
    );

  SELECT count(*) INTO typed_value_checks
  FROM pg_constraint
  WHERE conname IN (
      'chk_telemetry_raw_typed_value',
      'chk_telemetry_latest_raw_typed_value'
    )
    AND conrelid IN (
      'public.t_telemetry'::regclass,
      'public.t_telemetry_latest'::regclass
    );

  SELECT count(*) = 1 INTO cache_index_valid
  FROM pg_index AS index_entry
  JOIN pg_class AS index_relation
    ON index_relation.oid = index_entry.indexrelid
  JOIN pg_namespace AS index_schema
    ON index_schema.oid = index_relation.relnamespace
  WHERE index_schema.nspname = 'public'
    AND index_relation.relname = 'idx_l0_observation_dedup_created_at'
    AND index_entry.indrelid = 'public.t_l0_observation_dedup'::regclass
    AND pg_get_indexdef(index_entry.indexrelid)
      LIKE '%USING btree (created_at)%';

  SELECT count(*) = 1 INTO prune_procedure_valid
  FROM pg_proc AS procedure_entry
  JOIN pg_namespace AS procedure_schema
    ON procedure_schema.oid = procedure_entry.pronamespace
  WHERE procedure_schema.nspname = 'public'
    AND procedure_entry.proname = 'prune_l0_observation_dedup'
    AND procedure_entry.prokind = 'p'
    AND pg_get_function_identity_arguments(procedure_entry.oid)
      = 'IN job_id integer, IN config jsonb'
    AND pg_get_functiondef(procedure_entry.oid)
      LIKE '%clock_timestamp() - interval ''6 hours''%';

  SELECT count(*) = 1
         AND bool_and(
           schedule_interval = interval '15 minutes'
           AND config = '{}'::jsonb
           AND scheduled
         )
    INTO prune_job_valid
  FROM timescaledb_information.jobs
  WHERE proc_schema = 'public'
    AND proc_name = 'prune_l0_observation_dedup';

  SELECT count(*) = 1
         AND bool_and(time_interval = interval '1 hour')
    INTO telemetry_dimension_valid
  FROM timescaledb_information.dimensions
  WHERE hypertable_schema = 'public'
    AND hypertable_name = 't_telemetry';

  SELECT compression_enabled
    INTO telemetry_compression_enabled
  FROM timescaledb_information.hypertables
  WHERE hypertable_schema = 'public'
    AND hypertable_name = 't_telemetry';

  SELECT count(*) = 4
         AND count(*) FILTER (
           WHERE proc_name = 'policy_compression'
             AND logical_name = 't_telemetry'
             AND (config->>'compress_after')::interval = interval '6 hours'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_retention'
             AND logical_name = 't_telemetry'
             AND (config->>'drop_after')::interval = interval '7 days'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_refresh_continuous_aggregate'
             AND logical_name = 'tel_agg_1h'
             AND schedule_interval = interval '1 hour'
             AND initial_start IS NOT NULL
             AND fixed_schedule
             AND (config->>'start_offset')::interval = interval '8 days'
             AND (config->>'end_offset')::interval = interval '1 hour'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_refresh_continuous_aggregate'
             AND logical_name = 'tel_agg_1d'
             AND schedule_interval = interval '1 day'
             AND initial_start IS NOT NULL
             AND fixed_schedule
             AND (config->>'start_offset')::interval = interval '8 days'
             AND (config->>'end_offset')::interval = interval '1 day'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_refresh_continuous_aggregate'
             AND logical_name = 'tel_agg_5min'
         ) = 0
    INTO storage_jobs_valid
  FROM (
    SELECT jobs.proc_name,
           COALESCE(cagg.view_name, jobs.hypertable_name) AS logical_name,
           jobs.schedule_interval,
           jobs.initial_start,
           jobs.fixed_schedule,
           jobs.config
    FROM timescaledb_information.jobs AS jobs
    LEFT JOIN timescaledb_information.continuous_aggregates AS cagg
      ON cagg.materialization_hypertable_schema = jobs.hypertable_schema
     AND cagg.materialization_hypertable_name = jobs.hypertable_name
    WHERE jobs.proc_name IN (
        'policy_compression',
        'policy_retention',
        'policy_refresh_continuous_aggregate'
      )
      AND COALESCE(cagg.view_name, jobs.hypertable_name) IN (
        't_telemetry', 'tel_agg_5min', 'tel_agg_1h', 'tel_agg_1d'
      )
  ) AS governed_jobs;

  final_contract :=
    history_foreign_keys = 0
    AND typed_value_checks = 2
    AND cache_index_valid
    AND prune_procedure_valid
    AND prune_job_valid
    AND telemetry_dimension_valid
    AND telemetry_compression_enabled
    AND storage_jobs_valid
    AND to_regclass(
      'public.t_l0_observation_dedup_044_retired'
    ) IS NULL
    AND NOT EXISTS (
      SELECT 1
      FROM pg_trigger
      WHERE tgrelid = 'public.t_l0_observation_dedup'::regclass
        AND tgname = 'trg_t_l0_observation_dedup_append_only'
        AND NOT tgisinternal
    );

  IF final_contract THEN
    RETURN;
  END IF;

  IF history_foreign_keys <> 3
     OR typed_value_checks <> 2
     OR cache_index_valid
     OR to_regprocedure(
       'public.prune_l0_observation_dedup(integer,jsonb)'
     ) IS NOT NULL
     OR (
       SELECT count(*)
       FROM timescaledb_information.jobs
       WHERE proc_schema = 'public'
         AND proc_name = 'prune_l0_observation_dedup'
     ) <> 0
     OR to_regclass(
       'public.t_l0_observation_dedup_044_retired'
     ) IS NOT NULL THEN
    RAISE EXCEPTION
      'SCHEMA_045_PARTIAL_STRUCTURE: schema 045 footprint is malformed'
      USING ERRCODE = '55000';
  END IF;

  EXECUTE 'ALTER TABLE public.t_telemetry '
          'DROP CONSTRAINT IF EXISTS fk_telemetry_l0_observation';
  EXECUTE 'ALTER TABLE public.t_telemetry_latest '
          'DROP CONSTRAINT IF EXISTS fk_telemetry_latest_l0_observation';
  EXECUTE 'ALTER TABLE public.t_l2_observation_sources '
          'DROP CONSTRAINT IF EXISTS '
          't_l2_observation_sources_l0_observation_id_fkey';
  EXECUTE 'ALTER TABLE public.t_l0_observation_dedup '
          'RENAME TO t_l0_observation_dedup_044_retired';
  EXECUTE $ddl$
    CREATE TABLE public.t_l0_observation_dedup (
      observation_id UUID PRIMARY KEY,
      tag_id UUID NOT NULL REFERENCES public.t_tags(id),
      observed_at TIMESTAMPTZ NOT NULL,
      source_digest CHAR(64) NOT NULL UNIQUE,
      source_message_id TEXT,
      source_sequence BIGINT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
  $ddl$;
  EXECUTE $ddl$
    INSERT INTO public.t_l0_observation_dedup
      (observation_id, tag_id, observed_at, source_digest,
       source_message_id, source_sequence, created_at)
    SELECT observation_id, tag_id, observed_at, source_digest,
           source_message_id, source_sequence, created_at
    FROM public.t_l0_observation_dedup_044_retired
    WHERE created_at >= clock_timestamp() - interval '6 hours'
  $ddl$;
  EXECUTE 'CREATE INDEX idx_l0_observation_dedup_created_at '
          'ON public.t_l0_observation_dedup(created_at)';
  EXECUTE 'DROP TABLE public.t_l0_observation_dedup_044_retired';

  EXECUTE $ddl$
    CREATE PROCEDURE public.prune_l0_observation_dedup(
      job_id INTEGER,
      config JSONB
    )
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
    AS $procedure$
    BEGIN
      DELETE FROM public.t_l0_observation_dedup
      WHERE created_at < clock_timestamp() - interval '6 hours';
    END;
    $procedure$
  $ddl$;

  -- TimescaleDB 2.29 add_job has no if_not_exists argument. The old-schema
  -- branch above proves absence, while replay returns before this call.
  PERFORM public.add_job(
    'public.prune_l0_observation_dedup',
    interval '15 minutes',
    config => '{}'::jsonb
  );

  PERFORM public.set_chunk_time_interval(
    'public.t_telemetry',
    interval '1 hour'
  );
  PERFORM public.remove_compression_policy(
    'public.t_telemetry',
    if_exists => TRUE
  );
  PERFORM public.remove_retention_policy(
    'public.t_telemetry',
    if_exists => TRUE
  );
  PERFORM public.remove_continuous_aggregate_policy(
    'public.tel_agg_5min',
    if_exists => TRUE
  );
  PERFORM public.remove_continuous_aggregate_policy(
    'public.tel_agg_1h',
    if_exists => TRUE
  );
  PERFORM public.remove_continuous_aggregate_policy(
    'public.tel_agg_1d',
    if_exists => TRUE
  );

  IF NOT telemetry_compression_enabled THEN
    EXECUTE 'ALTER TABLE public.t_telemetry SET ('
            'timescaledb.compress, '
            'timescaledb.compress_segmentby = ''node_id,tag_id'', '
            'timescaledb.compress_orderby = ''ts DESC'')';
  END IF;

  PERFORM public.add_compression_policy(
    'public.t_telemetry',
    interval '6 hours',
    if_not_exists => TRUE
  );
  PERFORM public.add_retention_policy(
    'public.t_telemetry',
    interval '7 days',
    if_not_exists => TRUE
  );
  PERFORM public.add_continuous_aggregate_policy(
    'public.tel_agg_1h',
    start_offset => interval '8 days',
    end_offset => interval '1 hour',
    schedule_interval => interval '1 hour',
    initial_start => clock_timestamp() + interval '1 minute',
    if_not_exists => TRUE
  );
  PERFORM public.add_continuous_aggregate_policy(
    'public.tel_agg_1d',
    start_offset => interval '8 days',
    end_offset => interval '1 day',
    schedule_interval => interval '1 day',
    initial_start => clock_timestamp() + interval '1 minute',
    if_not_exists => TRUE
  );

  SELECT count(*) = 1
         AND bool_and(
           schedule_interval = interval '15 minutes'
           AND config = '{}'::jsonb
           AND scheduled
         )
    INTO prune_job_valid
  FROM timescaledb_information.jobs
  WHERE proc_schema = 'public'
    AND proc_name = 'prune_l0_observation_dedup';

  SELECT count(*) = 1
         AND bool_and(time_interval = interval '1 hour')
    INTO telemetry_dimension_valid
  FROM timescaledb_information.dimensions
  WHERE hypertable_schema = 'public'
    AND hypertable_name = 't_telemetry';

  SELECT compression_enabled
    INTO telemetry_compression_enabled
  FROM timescaledb_information.hypertables
  WHERE hypertable_schema = 'public'
    AND hypertable_name = 't_telemetry';

  SELECT count(*) = 4
         AND count(*) FILTER (
           WHERE proc_name = 'policy_compression'
             AND logical_name = 't_telemetry'
             AND (config->>'compress_after')::interval = interval '6 hours'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_retention'
             AND logical_name = 't_telemetry'
             AND (config->>'drop_after')::interval = interval '7 days'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_refresh_continuous_aggregate'
             AND logical_name = 'tel_agg_1h'
             AND schedule_interval = interval '1 hour'
             AND initial_start IS NOT NULL
             AND fixed_schedule
             AND (config->>'start_offset')::interval = interval '8 days'
             AND (config->>'end_offset')::interval = interval '1 hour'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_refresh_continuous_aggregate'
             AND logical_name = 'tel_agg_1d'
             AND schedule_interval = interval '1 day'
             AND initial_start IS NOT NULL
             AND fixed_schedule
             AND (config->>'start_offset')::interval = interval '8 days'
             AND (config->>'end_offset')::interval = interval '1 day'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_refresh_continuous_aggregate'
             AND logical_name = 'tel_agg_5min'
         ) = 0
    INTO storage_jobs_valid
  FROM (
    SELECT jobs.proc_name,
           COALESCE(cagg.view_name, jobs.hypertable_name) AS logical_name,
           jobs.schedule_interval,
           jobs.initial_start,
           jobs.fixed_schedule,
           jobs.config
    FROM timescaledb_information.jobs AS jobs
    LEFT JOIN timescaledb_information.continuous_aggregates AS cagg
      ON cagg.materialization_hypertable_schema = jobs.hypertable_schema
     AND cagg.materialization_hypertable_name = jobs.hypertable_name
    WHERE jobs.proc_name IN (
        'policy_compression',
        'policy_retention',
        'policy_refresh_continuous_aggregate'
      )
      AND COALESCE(cagg.view_name, jobs.hypertable_name) IN (
        't_telemetry', 'tel_agg_5min', 'tel_agg_1h', 'tel_agg_1d'
      )
  ) AS governed_jobs;

  IF NOT (
    prune_job_valid
    AND telemetry_dimension_valid
    AND telemetry_compression_enabled
    AND storage_jobs_valid
  ) THEN
    RAISE EXCEPTION
      'SCHEMA_045_PARTIAL_STRUCTURE: fixed storage policies were not installed'
      USING ERRCODE = '55000';
  END IF;
END;
$migration$;

COMMIT;
