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

COMMIT;
