-- Schema 055: remove duplicate L0 history indexes from the one-second write path.

BEGIN;

DO $migration$
DECLARE
  has_authoritative_dedup BOOLEAN;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-055'));

  IF to_regclass('public.t_data_frames') IS NULL
     OR to_regclass('public.t_telemetry') IS NULL
     OR to_regclass('public.t_l0_observation_dedup') IS NULL THEN
    RAISE EXCEPTION
      'SCHEMA_055_REQUIRES_046: committed L0 storage is missing';
  END IF;

  SELECT EXISTS (
    SELECT 1
    FROM pg_index AS definition
    WHERE definition.indrelid='public.t_l0_observation_dedup'::regclass
      AND definition.indisunique
      AND (
        SELECT array_agg(attribute.attname::TEXT ORDER BY key.ordinality)
        FROM unnest(definition.indkey)
             WITH ORDINALITY AS key(attnum,ordinality)
        JOIN pg_attribute AS attribute
          ON attribute.attrelid=definition.indrelid
         AND attribute.attnum=key.attnum
      )=ARRAY['source_digest']::TEXT[]
  ) INTO has_authoritative_dedup;

  IF NOT has_authoritative_dedup THEN
    RAISE EXCEPTION
      'SCHEMA_055_PARTIAL_STRUCTURE: authoritative L0 dedup index is missing';
  END IF;

  -- source_digest is already globally unique in t_l0_observation_dedup.
  DROP INDEX IF EXISTS public.uq_telemetry_source_observation;

  -- tag_id is globally unique; keep one compact history index, never both forms.
  CREATE INDEX IF NOT EXISTS idx_tel_tag_ts
    ON public.t_telemetry(tag_id, ts DESC);
  DROP INDEX IF EXISTS public.idx_tel_node_tag;
END
$migration$;

COMMIT;
