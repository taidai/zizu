-- Schema 047: make every terminal-frame outbox row self-contained.

BEGIN;

DO $migration$
DECLARE
  has_payload_version BOOLEAN;
  has_payload BOOLEAN;
  contract_valid BOOLEAN;
  outbox_rows BIGINT;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-047'));

  IF to_regclass('public.t_data_frames') IS NULL
     OR to_regclass('public.t_data_frame_outbox') IS NULL THEN
    RAISE EXCEPTION 'SCHEMA_047_REQUIRES_046: data-frame schema is missing';
  END IF;

  SELECT EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema='public'
             AND table_name='t_data_frame_outbox'
             AND column_name='payload_version'
         ),
         EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema='public'
             AND table_name='t_data_frame_outbox'
             AND column_name='payload'
         )
    INTO has_payload_version, has_payload;

  IF has_payload_version OR has_payload THEN
    SELECT has_payload_version
       AND has_payload
       AND EXISTS (
         SELECT 1 FROM information_schema.columns
         WHERE table_schema='public'
           AND table_name='t_data_frame_outbox'
           AND column_name='payload_version'
           AND is_nullable='NO'
           AND column_default IS NULL
       )
       AND EXISTS (
         SELECT 1 FROM information_schema.columns
         WHERE table_schema='public'
           AND table_name='t_data_frame_outbox'
           AND column_name='payload'
           AND is_nullable='NO'
           AND column_default IS NULL
       )
       AND EXISTS (
         SELECT 1 FROM pg_indexes
         WHERE schemaname='public'
           AND indexname='ix_data_frame_outbox_replay'
       )
       AND EXISTS (
         SELECT 1 FROM pg_trigger
         WHERE tgrelid='public.t_data_frame_outbox'::regclass
           AND tgname='trg_guard_data_frame_outbox_payload'
           AND NOT tgisinternal
       )
      INTO contract_valid;
    IF contract_valid THEN
      RETURN;
    END IF;
    RAISE EXCEPTION 'SCHEMA_047_PARTIAL_STRUCTURE: damaged frame payload schema';
  END IF;

  SELECT count(*) INTO outbox_rows FROM public.t_data_frame_outbox;
  IF outbox_rows <> 0 THEN
    RAISE EXCEPTION 'SCHEMA_047_OUTBOX_NOT_EMPTY: drain or clear old frame outbox first';
  END IF;

  ALTER TABLE public.t_data_frame_outbox
    ADD COLUMN payload_version SMALLINT NOT NULL DEFAULT 1,
    ADD COLUMN payload JSONB NOT NULL DEFAULT '{}'::jsonb;
  ALTER TABLE public.t_data_frame_outbox
    ALTER COLUMN payload_version DROP DEFAULT,
    ALTER COLUMN payload DROP DEFAULT;
  ALTER TABLE public.t_data_frame_outbox
    ADD CONSTRAINT chk_data_frame_outbox_payload_version
      CHECK (payload_version = 1),
    ADD CONSTRAINT chk_data_frame_outbox_payload_shape CHECK (
      jsonb_typeof(payload) = 'object'
      AND payload->>'type' = 'frame_delta'
      AND payload ? 'frame_id'
      AND payload ? 'frame_sequence'
      AND payload ? 'status'
      AND payload ? 'frame_time'
      AND payload ? 'configuration_revision'
      AND jsonb_typeof(payload->'l0_changes') = 'array'
      AND jsonb_typeof(payload->'l2_changes') = 'array'
      AND payload ? 'failure'
    );
  CREATE INDEX ix_data_frame_outbox_replay
    ON public.t_data_frame_outbox(frame_sequence)
    WHERE published_at IS NOT NULL;
END
$migration$;

CREATE OR REPLACE FUNCTION public.guard_data_frame_outbox_payload()
RETURNS TRIGGER LANGUAGE plpgsql AS $function$
BEGIN
  IF NEW.payload_version IS DISTINCT FROM OLD.payload_version
     OR NEW.payload IS DISTINCT FROM OLD.payload THEN
    RAISE EXCEPTION 'DATA_FRAME_OUTBOX_PAYLOAD_IMMUTABLE: committed payload cannot change'
      USING ERRCODE = '55000';
  END IF;
  RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS trg_guard_data_frame_outbox_payload
  ON public.t_data_frame_outbox;
CREATE TRIGGER trg_guard_data_frame_outbox_payload
BEFORE UPDATE ON public.t_data_frame_outbox
FOR EACH ROW EXECUTE FUNCTION public.guard_data_frame_outbox_payload();

COMMIT;
