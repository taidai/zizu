-- Schema 059: preserve L0 raw scalar quality evidence.

BEGIN;

DO $migration$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-059'));

  IF to_regclass('public.t_telemetry') IS NULL
     OR to_regclass('public.t_telemetry_latest') IS NULL
     OR to_regclass('public.t_l2_latest') IS NULL THEN
    RAISE EXCEPTION 'SCHEMA_059_REQUIRES_DATA_TRUNK_RUNTIME';
  END IF;
END
$migration$;

ALTER TABLE public.t_telemetry
  ADD COLUMN IF NOT EXISTS quality_reason TEXT;

ALTER TABLE public.t_telemetry_latest
  ADD COLUMN IF NOT EXISTS quality_reason TEXT;

ALTER TABLE public.t_l2_latest
  ADD COLUMN IF NOT EXISTS value_observed_at TIMESTAMPTZ;

UPDATE public.t_l2_latest
SET value_observed_at=observed_at
WHERE quality=192
  AND value_observed_at IS NULL
  AND num_nonnulls(
    value_float,value_int,value_numeric,value_bool,value_text,value_codes
  ) = 1;

ALTER TABLE public.t_telemetry
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

ALTER TABLE public.t_telemetry_latest
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

ALTER TABLE public.t_l2_latest
  DROP CONSTRAINT IF EXISTS chk_l2_latest_typed_value;
ALTER TABLE public.t_l2_latest
  ADD CONSTRAINT chk_l2_latest_typed_value CHECK (
    (
      quality=192
      AND value_observed_at IS NOT NULL
      AND num_nonnulls(
        value_float,value_int,value_numeric,value_bool,value_text,value_codes
      ) = 1
    )
    OR
    (
      quality IN (0,1,64)
      AND (
        (
          value_observed_at IS NULL
          AND num_nonnulls(
            value_float,value_int,value_numeric,value_bool,value_text,value_codes
          ) = 0
        )
        OR
        (
          value_observed_at IS NOT NULL
          AND num_nonnulls(
            value_float,value_int,value_numeric,value_bool,value_text,value_codes
          ) = 1
        )
      )
    )
  ) NOT VALID;

CREATE OR REPLACE FUNCTION public.validate_l2_typed_value_against_entity()
RETURNS TRIGGER LANGUAGE plpgsql AS $function$
DECLARE
  expected_type TEXT;
  value_count INTEGER;
  value_matches BOOLEAN;
  is_latest BOOLEAN;
  retained_value_time TIMESTAMPTZ;
BEGIN
  SELECT data_type INTO expected_type
  FROM public.t_entity_instances WHERE id=NEW.entity_instance_id;
  IF expected_type IS NULL THEN
    RETURN NEW;
  END IF;
  value_count := num_nonnulls(
    NEW.value_float,NEW.value_int,NEW.value_numeric,
    NEW.value_bool,NEW.value_text,NEW.value_codes
  );
  is_latest := TG_TABLE_NAME='t_l2_latest';
  retained_value_time := CASE
    WHEN is_latest
      THEN NULLIF(to_jsonb(NEW)->>'value_observed_at','')::timestamptz
    ELSE NULL
  END;
  IF is_latest THEN
    IF value_count=0 THEN
      value_matches := NEW.quality IN (0,1,64)
        AND retained_value_time IS NULL;
    ELSE
      value_matches := value_count=1
        AND retained_value_time IS NOT NULL
        AND CASE expected_type
          WHEN 'FLOAT' THEN NEW.value_float IS NOT NULL OR NEW.value_numeric IS NOT NULL
          WHEN 'INT' THEN NEW.value_int IS NOT NULL OR NEW.value_numeric IS NOT NULL
          WHEN 'BOOL' THEN NEW.value_bool IS NOT NULL
          WHEN 'STRING' THEN NEW.value_text IS NOT NULL
          WHEN 'ENUM' THEN NEW.value_text IS NOT NULL
          WHEN 'CODE_SET' THEN NEW.value_codes IS NOT NULL
          ELSE FALSE
        END;
    END IF;
  ELSIF NEW.quality=0 THEN
    value_matches := value_count=0;
  ELSIF NEW.quality=1 AND value_count=0 THEN
    value_matches := NEW.reason='FRAME_PROCESSING_FAILED_NO_BASELINE';
  ELSE
    value_matches := value_count=1 AND CASE expected_type
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
    RAISE EXCEPTION 'L2 typed value does not match entity data_type %',expected_type
      USING ERRCODE='23514',CONSTRAINT='chk_l2_entity_data_type';
  END IF;
  RETURN NEW;
END
$function$;

CREATE INDEX IF NOT EXISTS ix_l2_observations_entity_good_commit
  ON public.t_l2_observations(entity_instance_id,commit_sequence DESC)
  WHERE quality=192;

CREATE TABLE IF NOT EXISTS public.t_point_processing_passthrough_rules (
  output_id UUID PRIMARY KEY
    REFERENCES public.t_point_processing_outputs(id) ON DELETE CASCADE,
  input_id UUID NOT NULL
    REFERENCES public.t_point_processing_inputs(id)
);

CREATE TABLE IF NOT EXISTS public.t_point_processing_boolean_map_rules (
  output_id UUID PRIMARY KEY
    REFERENCES public.t_point_processing_outputs(id) ON DELETE CASCADE,
  input_id UUID NOT NULL
    REFERENCES public.t_point_processing_inputs(id),
  true_when SMALLINT NOT NULL CHECK (true_when IN (0, 1)),
  compiled_ast JSONB NOT NULL,
  ast_digest CHAR(64) NOT NULL
);

COMMIT;
