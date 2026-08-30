-- Schema 054: keep the original L0 acceptance beat in the latest projection.

BEGIN;

DO $migration$
DECLARE
  accepted_beat_type TEXT;
  accepted_beat_constraint TEXT;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-054'));

  IF to_regclass('public.t_telemetry_latest') IS NULL
     OR to_regclass('public.t_telemetry') IS NULL
     OR to_regclass('public.t_data_frames') IS NULL
     OR to_regclass('public.ix_data_frames_claim') IS NULL
     OR (
       SELECT count(*)
       FROM information_schema.columns
       WHERE table_schema='public'
         AND (
           (table_name='t_telemetry'
            AND column_name IN ('observation_id','ts','accepted_beat'))
           OR (table_name='t_telemetry_latest'
               AND column_name IN ('observation_id','ts'))
         )
     )<>5 THEN
    RAISE EXCEPTION
      'SCHEMA_054_REQUIRES_046: committed L0 storage is missing';
  END IF;

  SELECT format_type(attribute.atttypid,attribute.atttypmod)
    INTO accepted_beat_type
  FROM pg_attribute AS attribute
  WHERE attribute.attrelid='public.t_telemetry_latest'::regclass
    AND attribute.attname='accepted_beat'
    AND NOT attribute.attisdropped;

  IF accepted_beat_type IS NULL THEN
    ALTER TABLE public.t_telemetry_latest
      ADD COLUMN accepted_beat BIGINT;
  ELSIF accepted_beat_type<>'bigint' THEN
    RAISE EXCEPTION
      'SCHEMA_054_PARTIAL_STRUCTURE: accepted_beat has an unexpected type';
  END IF;

  UPDATE public.t_telemetry_latest AS latest
  SET accepted_beat=source.accepted_beat
  FROM public.t_telemetry AS source
  WHERE latest.accepted_beat IS NULL
    AND source.observation_id=latest.observation_id
    AND source.ts=latest.ts
    AND source.accepted_beat IS NOT NULL;

  -- Rows without matching committed evidence have no provable acceptance beat.
  -- Zero deliberately keeps them fail-closed until a new observation arrives.
  UPDATE public.t_telemetry_latest
  SET accepted_beat=0
  WHERE accepted_beat IS NULL;

  ALTER TABLE public.t_telemetry_latest
    ALTER COLUMN accepted_beat SET NOT NULL;

  SELECT pg_get_constraintdef(constraint_row.oid)
    INTO accepted_beat_constraint
  FROM pg_constraint AS constraint_row
  WHERE constraint_row.conrelid='public.t_telemetry_latest'::regclass
    AND constraint_row.conname='chk_telemetry_latest_accepted_beat';

  IF accepted_beat_constraint IS NULL THEN
    ALTER TABLE public.t_telemetry_latest
      ADD CONSTRAINT chk_telemetry_latest_accepted_beat
      CHECK (accepted_beat >= 0);
  ELSIF position('accepted_beat >= 0' IN accepted_beat_constraint)=0 THEN
    RAISE EXCEPTION
      'SCHEMA_054_PARTIAL_STRUCTURE: accepted_beat constraint is damaged';
  END IF;
END
$migration$;

COMMIT;
