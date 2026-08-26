-- Schema 045: bound edge telemetry storage while preserving source evidence.

BEGIN;

DO $migration$
DECLARE
  required_objects INTEGER;
  history_foreign_keys INTEGER;
  typed_value_checks INTEGER;
  cache_columns_valid BOOLEAN;
  cache_constraints_valid BOOLEAN;
  cache_table_valid BOOLEAN;
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

  SELECT count(*) = 7
         AND bool_and(
           (attnum = 1 AND attname = 'observation_id'
             AND data_type = 'uuid' AND attnotnull AND default_expr IS NULL)
           OR (attnum = 2 AND attname = 'tag_id'
             AND data_type = 'uuid' AND attnotnull AND default_expr IS NULL)
           OR (attnum = 3 AND attname = 'observed_at'
             AND data_type = 'timestamp with time zone'
             AND attnotnull AND default_expr IS NULL)
           OR (attnum = 4 AND attname = 'source_digest'
             AND data_type = 'character(64)'
             AND attnotnull AND default_expr IS NULL)
           OR (attnum = 5 AND attname = 'source_message_id'
             AND data_type = 'text'
             AND NOT attnotnull AND default_expr IS NULL)
           OR (attnum = 6 AND attname = 'source_sequence'
             AND data_type = 'bigint'
             AND NOT attnotnull AND default_expr IS NULL)
           OR (attnum = 7 AND attname = 'created_at'
             AND data_type = 'timestamp with time zone'
             AND attnotnull
             AND default_expr IS NOT DISTINCT FROM 'now()')
         )
    INTO cache_columns_valid
  FROM (
    SELECT attribute.attnum,
           attribute.attname,
           format_type(attribute.atttypid, attribute.atttypmod) AS data_type,
           attribute.attnotnull,
           pg_get_expr(default_entry.adbin, default_entry.adrelid)
             AS default_expr
    FROM pg_attribute AS attribute
    LEFT JOIN pg_attrdef AS default_entry
      ON default_entry.adrelid = attribute.attrelid
     AND default_entry.adnum = attribute.attnum
    WHERE attribute.attrelid =
            'public.t_l0_observation_dedup'::regclass
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
  ) AS cache_columns;

  SELECT count(*) = 3
         AND count(*) FILTER (
           WHERE contype = 'p'
             AND pg_get_constraintdef(oid) =
               'PRIMARY KEY (observation_id)'
         ) = 1
         AND count(*) FILTER (
           WHERE contype = 'u'
             AND pg_get_constraintdef(oid) = 'UNIQUE (source_digest)'
         ) = 1
         AND count(*) FILTER (
           WHERE contype = 'f'
             AND pg_get_constraintdef(oid) =
               'FOREIGN KEY (tag_id) REFERENCES t_tags(id)'
         ) = 1
    INTO cache_constraints_valid
  FROM pg_constraint
  WHERE conrelid = 'public.t_l0_observation_dedup'::regclass;

  cache_table_valid :=
    cache_columns_valid
    AND cache_constraints_valid
    AND EXISTS (
      SELECT 1
      FROM pg_class AS cache_relation
      JOIN pg_namespace AS cache_schema
        ON cache_schema.oid = cache_relation.relnamespace
      WHERE cache_schema.nspname = 'public'
        AND cache_relation.relname = 't_l0_observation_dedup'
        AND cache_relation.relkind = 'r'
        AND cache_relation.relpersistence = 'p'
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
  JOIN pg_language AS procedure_language
    ON procedure_language.oid = procedure_entry.prolang
  WHERE procedure_schema.nspname = 'public'
    AND procedure_entry.proname = 'prune_l0_observation_dedup'
    AND procedure_entry.prokind = 'p'
    AND procedure_language.lanname = 'plpgsql'
    AND procedure_entry.provolatile = 'v'
    AND NOT procedure_entry.prosecdef
    AND NOT procedure_entry.proisstrict
    AND procedure_entry.proconfig =
      ARRAY['search_path=pg_catalog, public']::text[]
    AND pg_get_function_identity_arguments(procedure_entry.oid)
      = 'IN job_id integer, IN config jsonb'
    AND btrim(regexp_replace(
      procedure_entry.prosrc,
      '[[:space:]]+',
      ' ',
      'g'
    )) = 'BEGIN DELETE FROM public.t_l0_observation_dedup '
        'WHERE created_at < clock_timestamp() - interval ''6 hours''; END;';

  SELECT count(*) = 1
         AND bool_and(
           jobs.schedule_interval = interval '15 minutes'
           AND jobs.max_runtime = interval '0'
           AND jobs.max_retries = -1
           AND jobs.retry_period = interval '5 minutes'
           AND jobs.config = '{}'::jsonb
           AND jobs.scheduled
           AND jobs.fixed_schedule
           AND jobs.initial_start IS NOT NULL
           AND jobs.hypertable_schema IS NULL
           AND jobs.hypertable_name IS NULL
           AND jobs.check_schema IS NULL
           AND jobs.check_name IS NULL
           AND (
             stats.job_status = 'Running'
             OR (
               stats.job_status = 'Scheduled'
               AND stats.next_start <= clock_timestamp()
                 + GREATEST(jobs.schedule_interval, jobs.retry_period)
             )
           )
         )
    INTO prune_job_valid
  FROM timescaledb_information.jobs AS jobs
  JOIN timescaledb_information.job_stats AS stats USING (job_id)
  WHERE jobs.proc_schema = 'public'
    AND jobs.proc_name = 'prune_l0_observation_dedup';

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
         AND bool_and(
           job_status = 'Running'
           OR (
             job_status = 'Scheduled'
             AND next_start <= clock_timestamp()
               + GREATEST(schedule_interval, retry_period)
           )
         )
         AND count(*) FILTER (
           WHERE proc_name = 'policy_compression'
             AND logical_name = 't_telemetry'
             AND proc_schema = '_timescaledb_functions'
             AND hypertable_schema = 'public'
             AND schedule_interval = interval '30 minutes'
             AND max_runtime = interval '0'
             AND max_retries = -1
             AND retry_period = interval '1 hour'
             AND scheduled
             AND NOT fixed_schedule
             AND initial_start IS NULL
             AND check_schema = '_timescaledb_functions'
             AND check_name = 'policy_compression_check'
             AND (SELECT count(*) FROM jsonb_object_keys(config)) = 2
             AND config ? 'hypertable_id'
             AND config ? 'compress_after'
             AND (config->>'compress_after')::interval = interval '6 hours'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_retention'
             AND logical_name = 't_telemetry'
             AND proc_schema = '_timescaledb_functions'
             AND hypertable_schema = 'public'
             AND schedule_interval = interval '1 day'
             AND max_runtime = interval '5 minutes'
             AND max_retries = -1
             AND retry_period = interval '5 minutes'
             AND scheduled
             AND NOT fixed_schedule
             AND initial_start IS NULL
             AND check_schema = '_timescaledb_functions'
             AND check_name = 'policy_retention_check'
             AND (SELECT count(*) FROM jsonb_object_keys(config)) = 2
             AND config ? 'hypertable_id'
             AND config ? 'drop_after'
             AND (config->>'drop_after')::interval = interval '7 days'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_refresh_continuous_aggregate'
             AND logical_name = 'tel_agg_1h'
             AND proc_schema = '_timescaledb_functions'
             AND hypertable_schema = 'public'
             AND schedule_interval = interval '1 hour'
             AND max_runtime = interval '0'
             AND max_retries = -1
             AND retry_period = interval '1 hour'
             AND scheduled
             AND initial_start IS NOT NULL
             AND fixed_schedule
             AND check_schema = '_timescaledb_functions'
             AND check_name = 'policy_refresh_continuous_aggregate_check'
             AND (SELECT count(*) FROM jsonb_object_keys(config)) = 3
             AND config ? 'mat_hypertable_id'
             AND config ? 'start_offset'
             AND config ? 'end_offset'
             AND (config->>'start_offset')::interval = interval '6 days'
             AND (config->>'end_offset')::interval = interval '1 hour'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_refresh_continuous_aggregate'
             AND logical_name = 'tel_agg_1d'
             AND proc_schema = '_timescaledb_functions'
             AND hypertable_schema = 'public'
             AND schedule_interval = interval '1 day'
             AND max_runtime = interval '0'
             AND max_retries = -1
             AND retry_period = interval '1 day'
             AND scheduled
             AND initial_start IS NOT NULL
             AND fixed_schedule
             AND check_schema = '_timescaledb_functions'
             AND check_name = 'policy_refresh_continuous_aggregate_check'
             AND (SELECT count(*) FROM jsonb_object_keys(config)) = 3
             AND config ? 'mat_hypertable_id'
             AND config ? 'start_offset'
             AND config ? 'end_offset'
             AND (config->>'start_offset')::interval = interval '6 days'
             AND (config->>'end_offset')::interval = interval '1 day'
         ) = 1
    INTO storage_jobs_valid
  FROM (
    SELECT jobs.proc_name,
           jobs.proc_schema,
           COALESCE(cagg.view_name, jobs.hypertable_name) AS logical_name,
           jobs.hypertable_schema,
           jobs.schedule_interval,
           jobs.max_runtime,
           jobs.max_retries,
           jobs.retry_period,
           jobs.scheduled,
           jobs.initial_start,
           jobs.fixed_schedule,
           jobs.config,
           jobs.check_schema,
           jobs.check_name,
           stats.job_status,
           stats.next_start
    FROM timescaledb_information.jobs AS jobs
    JOIN timescaledb_information.job_stats AS stats USING (job_id)
    LEFT JOIN timescaledb_information.continuous_aggregates AS cagg
      ON cagg.materialization_hypertable_schema = jobs.hypertable_schema
     AND cagg.materialization_hypertable_name = jobs.hypertable_name
    WHERE jobs.proc_name IN (
        'policy_compression',
        'policy_retention',
        'policy_refresh_continuous_aggregate'
      )
      AND COALESCE(cagg.view_name, jobs.hypertable_name) IN (
        't_telemetry', 'tel_agg_1h', 'tel_agg_1d'
      )
  ) AS governed_jobs;

  final_contract :=
    history_foreign_keys = 0
    AND typed_value_checks = 2
    AND cache_table_valid
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
     OR NOT cache_table_valid
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
    start_offset => interval '6 days',
    end_offset => interval '1 hour',
    schedule_interval => interval '1 hour',
    initial_start => clock_timestamp() + interval '1 minute',
    if_not_exists => TRUE
  );
  PERFORM public.add_continuous_aggregate_policy(
    'public.tel_agg_1d',
    start_offset => interval '6 days',
    end_offset => interval '1 day',
    schedule_interval => interval '1 day',
    initial_start => clock_timestamp() + interval '1 minute',
    if_not_exists => TRUE
  );

  SELECT count(*) = 1
         AND bool_and(
           jobs.schedule_interval = interval '15 minutes'
           AND jobs.max_runtime = interval '0'
           AND jobs.max_retries = -1
           AND jobs.retry_period = interval '5 minutes'
           AND jobs.config = '{}'::jsonb
           AND jobs.scheduled
           AND jobs.fixed_schedule
           AND jobs.initial_start IS NOT NULL
           AND jobs.hypertable_schema IS NULL
           AND jobs.hypertable_name IS NULL
           AND jobs.check_schema IS NULL
           AND jobs.check_name IS NULL
           AND (
             stats.job_status = 'Running'
             OR (
               stats.job_status = 'Scheduled'
               AND stats.next_start <= clock_timestamp()
                 + GREATEST(jobs.schedule_interval, jobs.retry_period)
             )
           )
         )
    INTO prune_job_valid
  FROM timescaledb_information.jobs AS jobs
  JOIN timescaledb_information.job_stats AS stats USING (job_id)
  WHERE jobs.proc_schema = 'public'
    AND jobs.proc_name = 'prune_l0_observation_dedup';

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
         AND bool_and(
           job_status = 'Running'
           OR (
             job_status = 'Scheduled'
             AND next_start <= clock_timestamp()
               + GREATEST(schedule_interval, retry_period)
           )
         )
         AND count(*) FILTER (
           WHERE proc_name = 'policy_compression'
             AND logical_name = 't_telemetry'
             AND proc_schema = '_timescaledb_functions'
             AND hypertable_schema = 'public'
             AND schedule_interval = interval '30 minutes'
             AND max_runtime = interval '0'
             AND max_retries = -1
             AND retry_period = interval '1 hour'
             AND scheduled
             AND NOT fixed_schedule
             AND initial_start IS NULL
             AND check_schema = '_timescaledb_functions'
             AND check_name = 'policy_compression_check'
             AND (SELECT count(*) FROM jsonb_object_keys(config)) = 2
             AND config ? 'hypertable_id'
             AND config ? 'compress_after'
             AND (config->>'compress_after')::interval = interval '6 hours'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_retention'
             AND logical_name = 't_telemetry'
             AND proc_schema = '_timescaledb_functions'
             AND hypertable_schema = 'public'
             AND schedule_interval = interval '1 day'
             AND max_runtime = interval '5 minutes'
             AND max_retries = -1
             AND retry_period = interval '5 minutes'
             AND scheduled
             AND NOT fixed_schedule
             AND initial_start IS NULL
             AND check_schema = '_timescaledb_functions'
             AND check_name = 'policy_retention_check'
             AND (SELECT count(*) FROM jsonb_object_keys(config)) = 2
             AND config ? 'hypertable_id'
             AND config ? 'drop_after'
             AND (config->>'drop_after')::interval = interval '7 days'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_refresh_continuous_aggregate'
             AND logical_name = 'tel_agg_1h'
             AND proc_schema = '_timescaledb_functions'
             AND hypertable_schema = 'public'
             AND schedule_interval = interval '1 hour'
             AND max_runtime = interval '0'
             AND max_retries = -1
             AND retry_period = interval '1 hour'
             AND scheduled
             AND initial_start IS NOT NULL
             AND fixed_schedule
             AND check_schema = '_timescaledb_functions'
             AND check_name = 'policy_refresh_continuous_aggregate_check'
             AND (SELECT count(*) FROM jsonb_object_keys(config)) = 3
             AND config ? 'mat_hypertable_id'
             AND config ? 'start_offset'
             AND config ? 'end_offset'
             AND (config->>'start_offset')::interval = interval '6 days'
             AND (config->>'end_offset')::interval = interval '1 hour'
         ) = 1
         AND count(*) FILTER (
           WHERE proc_name = 'policy_refresh_continuous_aggregate'
             AND logical_name = 'tel_agg_1d'
             AND proc_schema = '_timescaledb_functions'
             AND hypertable_schema = 'public'
             AND schedule_interval = interval '1 day'
             AND max_runtime = interval '0'
             AND max_retries = -1
             AND retry_period = interval '1 day'
             AND scheduled
             AND initial_start IS NOT NULL
             AND fixed_schedule
             AND check_schema = '_timescaledb_functions'
             AND check_name = 'policy_refresh_continuous_aggregate_check'
             AND (SELECT count(*) FROM jsonb_object_keys(config)) = 3
             AND config ? 'mat_hypertable_id'
             AND config ? 'start_offset'
             AND config ? 'end_offset'
             AND (config->>'start_offset')::interval = interval '6 days'
             AND (config->>'end_offset')::interval = interval '1 day'
         ) = 1
    INTO storage_jobs_valid
  FROM (
    SELECT jobs.proc_name,
           jobs.proc_schema,
           COALESCE(cagg.view_name, jobs.hypertable_name) AS logical_name,
           jobs.hypertable_schema,
           jobs.schedule_interval,
           jobs.max_runtime,
           jobs.max_retries,
           jobs.retry_period,
           jobs.scheduled,
           jobs.initial_start,
           jobs.fixed_schedule,
           jobs.config,
           jobs.check_schema,
           jobs.check_name,
           stats.job_status,
           stats.next_start
    FROM timescaledb_information.jobs AS jobs
    JOIN timescaledb_information.job_stats AS stats USING (job_id)
    LEFT JOIN timescaledb_information.continuous_aggregates AS cagg
      ON cagg.materialization_hypertable_schema = jobs.hypertable_schema
     AND cagg.materialization_hypertable_name = jobs.hypertable_name
    WHERE jobs.proc_name IN (
        'policy_compression',
        'policy_retention',
        'policy_refresh_continuous_aggregate'
      )
      AND COALESCE(cagg.view_name, jobs.hypertable_name) IN (
        't_telemetry', 'tel_agg_1h', 'tel_agg_1d'
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
