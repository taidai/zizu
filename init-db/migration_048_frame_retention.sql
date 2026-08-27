-- Schema 048: fixed L2 aggregates and bounded committed-frame retention.

BEGIN;

DO $migration$
DECLARE
  footprint INTEGER;
  contract_valid BOOLEAN;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-048'));

  IF to_regclass('public.t_data_frame_outbox') IS NULL
     OR NOT EXISTS (
       SELECT 1 FROM information_schema.columns
       WHERE table_schema='public' AND table_name='t_data_frame_outbox'
         AND column_name='payload' AND is_nullable='NO'
     ) THEN
    RAISE EXCEPTION 'SCHEMA_048_REQUIRES_047: committed frame payload is missing';
  END IF;

  SELECT count(*) INTO footprint
  FROM (VALUES
    (to_regclass('public.l2_agg_1h') IS NOT NULL),
    (to_regclass('public.l2_agg_1d') IS NOT NULL),
    (to_regprocedure(
      'public.prune_committed_frame_history(integer,jsonb)'
    ) IS NOT NULL),
    (to_regclass('zizu_internal.retention_guard') IS NOT NULL)
  ) AS objects(present)
  WHERE present;

  IF footprint > 0 THEN
    SELECT footprint = 4
       AND (
         SELECT count(*) FROM timescaledb_information.continuous_aggregates
         WHERE view_schema='public'
           AND view_name IN ('l2_agg_1h','l2_agg_1d')
       ) = 2
       AND (
         SELECT count(*) FROM timescaledb_information.jobs
         WHERE proc_schema='public'
           AND proc_name='prune_committed_frame_history'
       ) = 1
       AND EXISTS (
         SELECT 1 FROM pg_trigger
         WHERE tgrelid='public.t_data_frames'::regclass
           AND tgname='trg_guard_data_frame_transition'
           AND NOT tgisinternal
       )
      INTO contract_valid;
    IF contract_valid THEN
      RETURN;
    END IF;
    RAISE EXCEPTION 'SCHEMA_048_PARTIAL_STRUCTURE: damaged retention schema';
  END IF;

  CREATE SCHEMA IF NOT EXISTS zizu_internal;
  REVOKE ALL ON SCHEMA zizu_internal FROM PUBLIC;
  EXECUTE $ddl$
    CREATE TABLE zizu_internal.retention_guard (
      singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
      token UUID NOT NULL
    )
  $ddl$;
  INSERT INTO zizu_internal.retention_guard(singleton,token)
  VALUES(TRUE,gen_random_uuid());
  REVOKE ALL ON TABLE zizu_internal.retention_guard FROM PUBLIC;

  EXECUTE $ddl$
    CREATE MATERIALIZED VIEW public.l2_agg_1h
    WITH (timescaledb.continuous) AS
    SELECT time_bucket(interval '1 hour', observed_at) AS bucket,
           entity_instance_id,
           count(*)::bigint AS sample_count,
           count(*) FILTER (WHERE quality=192)::bigint AS good_count,
           count(*) FILTER (WHERE quality=64)::bigint AS uncertain_count,
           count(*) FILTER (WHERE quality=0)::bigint AS bad_count,
           count(*) FILTER (WHERE quality=1)::bigint AS stale_count,
           first(COALESCE(value_numeric::double precision,
                          value_float,value_int::double precision), observed_at)
             FILTER (WHERE COALESCE(value_numeric::double precision,
                                    value_float,value_int::double precision)
                              IS NOT NULL) AS numeric_first,
           last(COALESCE(value_numeric::double precision,
                         value_float,value_int::double precision), observed_at)
             FILTER (WHERE COALESCE(value_numeric::double precision,
                                    value_float,value_int::double precision)
                              IS NOT NULL) AS numeric_last,
           min(COALESCE(value_numeric::double precision,
                        value_float,value_int::double precision)) AS numeric_min,
           max(COALESCE(value_numeric::double precision,
                        value_float,value_int::double precision)) AS numeric_max,
           avg(COALESCE(value_numeric::double precision,
                        value_float,value_int::double precision)) AS numeric_avg,
           first(value_bool,observed_at)
             FILTER (WHERE value_bool IS NOT NULL) AS bool_first,
           last(value_bool,observed_at)
             FILTER (WHERE value_bool IS NOT NULL) AS bool_last,
           first(value_text,observed_at)
             FILTER (WHERE value_text IS NOT NULL) AS text_first,
           last(value_text,observed_at)
             FILTER (WHERE value_text IS NOT NULL) AS text_last,
           first(value_codes,observed_at)
             FILTER (WHERE value_codes IS NOT NULL) AS codes_first,
           last(value_codes,observed_at)
             FILTER (WHERE value_codes IS NOT NULL) AS codes_last
    FROM public.t_l2_observations
    GROUP BY bucket,entity_instance_id
    WITH NO DATA
  $ddl$;

  EXECUTE $ddl$
    CREATE MATERIALIZED VIEW public.l2_agg_1d
    WITH (timescaledb.continuous) AS
    SELECT time_bucket(interval '1 day', observed_at) AS bucket,
           entity_instance_id,
           count(*)::bigint AS sample_count,
           count(*) FILTER (WHERE quality=192)::bigint AS good_count,
           count(*) FILTER (WHERE quality=64)::bigint AS uncertain_count,
           count(*) FILTER (WHERE quality=0)::bigint AS bad_count,
           count(*) FILTER (WHERE quality=1)::bigint AS stale_count,
           first(COALESCE(value_numeric::double precision,
                          value_float,value_int::double precision), observed_at)
             FILTER (WHERE COALESCE(value_numeric::double precision,
                                    value_float,value_int::double precision)
                              IS NOT NULL) AS numeric_first,
           last(COALESCE(value_numeric::double precision,
                         value_float,value_int::double precision), observed_at)
             FILTER (WHERE COALESCE(value_numeric::double precision,
                                    value_float,value_int::double precision)
                              IS NOT NULL) AS numeric_last,
           min(COALESCE(value_numeric::double precision,
                        value_float,value_int::double precision)) AS numeric_min,
           max(COALESCE(value_numeric::double precision,
                        value_float,value_int::double precision)) AS numeric_max,
           avg(COALESCE(value_numeric::double precision,
                        value_float,value_int::double precision)) AS numeric_avg,
           first(value_bool,observed_at)
             FILTER (WHERE value_bool IS NOT NULL) AS bool_first,
           last(value_bool,observed_at)
             FILTER (WHERE value_bool IS NOT NULL) AS bool_last,
           first(value_text,observed_at)
             FILTER (WHERE value_text IS NOT NULL) AS text_first,
           last(value_text,observed_at)
             FILTER (WHERE value_text IS NOT NULL) AS text_last,
           first(value_codes,observed_at)
             FILTER (WHERE value_codes IS NOT NULL) AS codes_first,
           last(value_codes,observed_at)
             FILTER (WHERE value_codes IS NOT NULL) AS codes_last
    FROM public.t_l2_observations
    GROUP BY bucket,entity_instance_id
    WITH NO DATA
  $ddl$;

  EXECUTE $ddl$
    CREATE OR REPLACE FUNCTION public.reject_data_trunk_append_only()
    RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog, public, zizu_internal AS $function$
    DECLARE
      expected_token TEXT;
    BEGIN
      SELECT token::text INTO expected_token
      FROM zizu_internal.retention_guard WHERE singleton=TRUE;
      IF TG_OP='DELETE'
         AND TG_TABLE_NAME IN ('t_l2_observations','t_l2_observation_sources')
         AND current_setting('zizu.maintenance_retention',TRUE)=expected_token THEN
        RETURN NULL;
      END IF;
      RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE='55000';
    END
    $function$
  $ddl$;

  EXECUTE $ddl$
    CREATE OR REPLACE FUNCTION public.guard_data_frame_transition()
    RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER
    SET search_path=pg_catalog, public, zizu_internal AS $function$
    DECLARE
      expected_token TEXT;
    BEGIN
      SELECT token::text INTO expected_token
      FROM zizu_internal.retention_guard WHERE singleton=TRUE;
      IF TG_OP='DELETE' THEN
        IF current_setting('zizu.maintenance_retention',TRUE)=expected_token THEN
          RETURN OLD;
        END IF;
        RAISE EXCEPTION
          'DATA_FRAME_TERMINAL_IMMUTABLE: frame deletion is forbidden'
          USING ERRCODE='55000';
      END IF;
      IF OLD.status IN ('COMPLETE','FAILED') THEN
        RAISE EXCEPTION
          'DATA_FRAME_TERMINAL_IMMUTABLE: terminal frame is immutable'
          USING ERRCODE='55000';
      END IF;
      IF (NEW.frame_id,NEW.frame_sequence,NEW.candidate_digest,
          NEW.capture_beat,NEW.shot_at,NEW.configuration_revision,NEW.created_at)
         IS DISTINCT FROM
         (OLD.frame_id,OLD.frame_sequence,OLD.candidate_digest,
          OLD.capture_beat,OLD.shot_at,OLD.configuration_revision,OLD.created_at)
      THEN
        RAISE EXCEPTION
          'DATA_FRAME_IDENTITY_IMMUTABLE: frame identity is immutable'
          USING ERRCODE='55000';
      END IF;
      IF OLD.status='PENDING' AND NEW.status='PROCESSING'
         AND (NEW.attempt_count=OLD.attempt_count+1
              OR (clock_timestamp()-OLD.created_at>=interval '60 seconds'
                  AND NEW.attempt_count=OLD.attempt_count))
         AND NEW.processing_owner IS NOT NULL
         AND NEW.processing_token IS NOT NULL
         AND NEW.lease_until>clock_timestamp()
         AND NEW.finished_at IS NULL AND NEW.failure_code IS NULL THEN
        RETURN NEW;
      END IF;
      IF OLD.status='PROCESSING' AND NEW.status='PENDING'
         AND NEW.attempt_count=OLD.attempt_count
         AND NEW.processing_owner IS NULL AND NEW.processing_token IS NULL
         AND NEW.lease_until IS NULL AND NEW.finished_at IS NULL
         AND NEW.failure_code IS NULL THEN
        RETURN NEW;
      END IF;
      IF OLD.status='PROCESSING' AND NEW.status='PROCESSING'
         AND NEW.attempt_count=OLD.attempt_count
         AND NEW.processing_owner IS DISTINCT FROM OLD.processing_owner
         AND NEW.processing_token IS DISTINCT FROM OLD.processing_token
         AND NEW.lease_until>clock_timestamp() THEN
        RETURN NEW;
      END IF;
      IF OLD.status='PROCESSING' AND NEW.status='PROCESSING'
         AND NEW.processing_token IS NOT DISTINCT FROM OLD.processing_token THEN
        RAISE EXCEPTION
          'DATA_FRAME_LEASE_RENEWAL_FORBIDDEN: fixed lease cannot renew'
          USING ERRCODE='55000';
      END IF;
      IF OLD.status='PROCESSING' AND NEW.status IN ('COMPLETE','FAILED')
         AND NEW.attempt_count=OLD.attempt_count THEN
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'DATA_FRAME_TRANSITION_INVALID: % -> %',OLD.status,NEW.status
        USING ERRCODE='55000';
    END
    $function$
  $ddl$;

  EXECUTE $ddl$
    CREATE PROCEDURE public.prune_committed_frame_history(
      job_id INTEGER,
      config JSONB
    )
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path=pg_catalog, public
    AS $procedure$
    DECLARE
      maintenance_now TIMESTAMPTZ := COALESCE(
        NULLIF(config->>'now','')::timestamptz,
        clock_timestamp()
      );
      guard_token UUID;
    BEGIN
      PERFORM pg_advisory_xact_lock(
        pg_catalog.hashtextextended('zizu:committed-frame-retention',0)
      );
      SELECT token INTO guard_token
      FROM zizu_internal.retention_guard WHERE singleton=TRUE;
      PERFORM pg_catalog.set_config(
        'zizu.maintenance_retention',guard_token::text,TRUE
      );

      DELETE FROM public.t_data_frame_outbox AS outbox
      WHERE outbox.published_at IS NOT NULL
        AND outbox.claimed_by IS NULL
        AND outbox.claim_token IS NULL
        AND outbox.claimed_until IS NULL
        AND (
          outbox.created_at < maintenance_now-interval '1 hour'
          OR outbox.frame_id IN (
            SELECT older.frame_id
            FROM public.t_data_frame_outbox AS older
            WHERE older.published_at IS NOT NULL
            ORDER BY older.frame_sequence DESC
            OFFSET 5000
          )
        );

      DELETE FROM public.t_l2_observation_sources AS source
      USING public.t_l2_observations AS observation
      WHERE source.l2_event_id=observation.event_id
        AND source.l2_observed_at=observation.observed_at
        AND observation.observed_at < maintenance_now-interval '7 days'
        AND NOT EXISTS (
          SELECT 1 FROM public.t_ingestion_failures AS failure
          WHERE failure.frame_id=observation.frame_id
        )
        AND NOT EXISTS (
          SELECT 1 FROM public.t_l2_observation_sources AS reference
          WHERE reference.source_l2_event_id=observation.event_id
            AND reference.source_l2_observed_at=observation.observed_at
        );

      DELETE FROM public.t_l2_observations AS observation
      WHERE observation.observed_at < maintenance_now-interval '7 days'
        AND NOT EXISTS (
          SELECT 1 FROM public.t_ingestion_failures AS failure
          WHERE failure.frame_id=observation.frame_id
        )
        AND NOT EXISTS (
          SELECT 1 FROM public.t_l2_observation_sources AS source
          WHERE (source.l2_event_id=observation.event_id
                 AND source.l2_observed_at=observation.observed_at)
             OR (source.source_l2_event_id=observation.event_id
                 AND source.source_l2_observed_at=observation.observed_at)
        );

      DELETE FROM public.t_data_frames AS frame
      WHERE frame.status IN ('COMPLETE','FAILED')
        AND frame.finished_at < maintenance_now-interval '7 days'
        AND NOT EXISTS (
          SELECT 1 FROM public.t_data_frame_outbox AS outbox
          WHERE outbox.frame_id=frame.frame_id
        )
        AND NOT EXISTS (
          SELECT 1 FROM public.t_telemetry AS telemetry
          WHERE telemetry.frame_id=frame.frame_id
        )
        AND NOT EXISTS (
          SELECT 1 FROM public.t_l2_observations AS observation
          WHERE observation.frame_id=frame.frame_id
        )
        AND NOT EXISTS (
          SELECT 1 FROM public.t_ingestion_failures AS failure
          WHERE failure.frame_id=frame.frame_id
        );
    END
    $procedure$
  $ddl$;

  REVOKE ALL ON PROCEDURE public.prune_committed_frame_history(INTEGER,JSONB)
    FROM PUBLIC;

  PERFORM public.add_continuous_aggregate_policy(
    'public.l2_agg_1h',
    start_offset=>interval '6 days',
    end_offset=>interval '1 hour',
    schedule_interval=>interval '1 hour',
    initial_start=>clock_timestamp()+interval '1 minute',
    if_not_exists=>TRUE
  );
  PERFORM public.add_continuous_aggregate_policy(
    'public.l2_agg_1d',
    start_offset=>interval '6 days',
    end_offset=>interval '1 day',
    schedule_interval=>interval '1 day',
    initial_start=>clock_timestamp()+interval '1 minute',
    if_not_exists=>TRUE
  );
  PERFORM public.add_job(
    'public.prune_committed_frame_history',
    interval '1 hour',
    config=>'{}'::jsonb
  );
END
$migration$;

COMMIT;
