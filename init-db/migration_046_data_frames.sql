-- Schema 046: one authoritative data-frame ledger for L0 -> L1 -> L2.

BEGIN;

DO $migration$
DECLARE
  new_frame_exists BOOLEAN := to_regclass('public.t_data_frames') IS NOT NULL;
  new_outbox_exists BOOLEAN := to_regclass('public.t_data_frame_outbox') IS NOT NULL;
  old_outbox_exists BOOLEAN := to_regclass('public.t_l2_stream_outbox') IS NOT NULL;
  contract_valid BOOLEAN;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-046'));

  IF new_frame_exists OR new_outbox_exists OR NOT old_outbox_exists THEN
    SELECT new_frame_exists
       AND new_outbox_exists
       AND NOT old_outbox_exists
       AND EXISTS (
         SELECT 1 FROM pg_constraint
         WHERE conrelid = 'public.t_data_frames'::regclass
           AND conname = 'chk_data_frame_status'
       )
       AND EXISTS (
         SELECT 1 FROM pg_trigger
         WHERE tgrelid = 'public.t_data_frames'::regclass
           AND tgname = 'trg_guard_data_frame_transition'
           AND NOT tgisinternal
       )
       AND EXISTS (
         SELECT 1 FROM pg_indexes
         WHERE schemaname = 'public'
           AND indexname = 'ix_data_frame_outbox_pending'
       )
      INTO contract_valid;
    IF contract_valid THEN
      RETURN;
    END IF;
    RAISE EXCEPTION 'SCHEMA_046_PARTIAL_STRUCTURE: mixed or damaged frame schema'
      USING ERRCODE = '55000';
  END IF;

  IF to_regclass('public.t_configuration_revisions') IS NULL
     OR to_regclass('public.t_telemetry') IS NULL
     OR to_regclass('public.t_telemetry_latest') IS NULL
     OR to_regclass('public.t_l2_observations') IS NULL
     OR to_regclass('public.t_l2_latest') IS NULL
     OR to_regclass('public.t_ingestion_failures') IS NULL
     OR to_regclass('public.t_tags') IS NULL
     OR to_regclass('public.t_l0_observation_dedup') IS NULL
     OR to_regclass('public.t_solution_packages') IS NOT NULL THEN
    RAISE EXCEPTION 'SCHEMA_046_PARTIAL_STRUCTURE: complete schema 045 is required'
      USING ERRCODE = '55000';
  END IF;

  IF EXISTS (
    SELECT 1 FROM public.t_l2_stream_outbox WHERE published_at IS NULL
  ) THEN
    RAISE EXCEPTION 'SCHEMA_046_OUTBOX_NOT_DRAINED: publish legacy outbox first'
      USING ERRCODE = '55000';
  END IF;

  EXECUTE $ddl$
    CREATE TABLE public.t_data_frames (
      frame_id UUID PRIMARY KEY,
      frame_sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
      candidate_digest CHAR(64) NOT NULL
        CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
      capture_beat BIGINT NOT NULL UNIQUE CHECK (capture_beat >= 1),
      shot_at TIMESTAMPTZ NOT NULL,
      configuration_revision BIGINT NOT NULL
        REFERENCES public.t_configuration_revisions(revision),
      status TEXT NOT NULL,
      attempt_count SMALLINT NOT NULL DEFAULT 0
        CHECK (attempt_count BETWEEN 0 AND 3),
      processing_owner UUID,
      processing_token UUID,
      lease_until TIMESTAMPTZ,
      failure_code TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      finished_at TIMESTAMPTZ,
      CONSTRAINT chk_data_frame_status
        CHECK (status IN ('PENDING','PROCESSING','COMPLETE','FAILED')),
      CONSTRAINT uq_data_frame_identity_sequence UNIQUE (frame_id, frame_sequence),
      CONSTRAINT uq_data_frame_terminal_identity
        UNIQUE (frame_id, frame_sequence, status),
      CONSTRAINT chk_data_frame_terminal
        CHECK ((status IN ('COMPLETE','FAILED')) = (finished_at IS NOT NULL)),
      CONSTRAINT chk_data_frame_failure_code
        CHECK ((status = 'FAILED') = (failure_code IS NOT NULL)),
      CONSTRAINT chk_data_frame_processing_lease CHECK (
        (status = 'PROCESSING' AND processing_owner IS NOT NULL
          AND processing_token IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (status <> 'PROCESSING' AND processing_owner IS NULL
          AND processing_token IS NULL AND lease_until IS NULL)
      )
    )
  $ddl$;

  EXECUTE $ddl$
    CREATE INDEX ix_data_frames_claim
      ON public.t_data_frames(status, lease_until, frame_sequence)
      WHERE status IN ('PENDING','PROCESSING')
  $ddl$;

  EXECUTE $ddl$
    ALTER TABLE public.t_telemetry
      ADD COLUMN frame_id UUID,
      ADD COLUMN frame_sequence BIGINT CHECK (frame_sequence >= 1),
      ADD COLUMN accepted_beat BIGINT CHECK (accepted_beat >= 1),
      ADD COLUMN source_order_mode TEXT
        CHECK (source_order_mode IN ('sequence','observed_at','received_at')),
      ADD COLUMN source_receive_ordinal BIGINT CHECK (source_receive_ordinal >= 0)
  $ddl$;
  EXECUTE $ddl$
    ALTER TABLE public.t_telemetry
      ADD CONSTRAINT fk_telemetry_data_frame
        FOREIGN KEY (frame_id, frame_sequence)
        REFERENCES public.t_data_frames(frame_id, frame_sequence) NOT VALID,
      ADD CONSTRAINT chk_telemetry_frame_fields CHECK (
        (frame_id IS NULL AND frame_sequence IS NULL AND accepted_beat IS NULL
          AND source_order_mode IS NULL AND source_receive_ordinal IS NULL)
        OR
        (frame_id IS NOT NULL AND frame_sequence IS NOT NULL
          AND accepted_beat IS NOT NULL AND source_order_mode IS NOT NULL
          AND (source_order_mode <> 'received_at'
            OR source_receive_ordinal IS NOT NULL))
      ) NOT VALID
  $ddl$;

  EXECUTE $ddl$
    ALTER TABLE public.t_telemetry_latest
      ADD COLUMN frame_sequence BIGINT NOT NULL DEFAULT 0
        CHECK (frame_sequence >= 0),
      ADD COLUMN source_order_mode TEXT
        CHECK (source_order_mode IN ('sequence','observed_at','received_at')),
      ADD COLUMN source_receive_ordinal BIGINT CHECK (source_receive_ordinal >= 0),
      ADD CONSTRAINT chk_telemetry_latest_frame_fields CHECK (
        (frame_sequence = 0 AND source_order_mode IS NULL
          AND source_receive_ordinal IS NULL)
        OR
        (frame_sequence > 0 AND source_order_mode IS NOT NULL
          AND (source_order_mode <> 'received_at'
            OR source_receive_ordinal IS NOT NULL))
      )
  $ddl$;
  EXECUTE 'ALTER TABLE public.t_telemetry_latest ALTER COLUMN frame_sequence DROP DEFAULT';

  EXECUTE 'ALTER TABLE public.t_l2_observations ADD COLUMN frame_id UUID';
  EXECUTE $ddl$
    ALTER TABLE public.t_l2_observations
      ADD CONSTRAINT fk_l2_observation_data_frame
        FOREIGN KEY (frame_id, commit_sequence)
        REFERENCES public.t_data_frames(frame_id, frame_sequence) NOT VALID
  $ddl$;
  EXECUTE $ddl$
    ALTER TABLE public.t_l2_latest
      ADD COLUMN frame_sequence BIGINT NOT NULL DEFAULT 0 CHECK (frame_sequence >= 0)
  $ddl$;
  EXECUTE 'ALTER TABLE public.t_l2_latest ALTER COLUMN frame_sequence DROP DEFAULT';
  EXECUTE 'ALTER TABLE public.t_l2_observations ALTER COLUMN commit_sequence DROP DEFAULT';
  EXECUTE 'ALTER TABLE public.t_tags ADD COLUMN source_sequence_trusted BOOLEAN NOT NULL DEFAULT FALSE';

  EXECUTE $ddl$
    CREATE INDEX ix_telemetry_frame_tag
      ON public.t_telemetry(frame_id, tag_id) WHERE frame_id IS NOT NULL
  $ddl$;
  EXECUTE $ddl$
    CREATE INDEX ix_telemetry_tag_frame_sequence
      ON public.t_telemetry(tag_id, frame_sequence DESC, ts DESC)
      WHERE frame_sequence IS NOT NULL
  $ddl$;
  EXECUTE $ddl$
    CREATE INDEX ix_l2_observations_frame
      ON public.t_l2_observations(frame_id, entity_instance_id)
      WHERE frame_id IS NOT NULL
  $ddl$;

  EXECUTE 'ALTER TABLE public.t_ingestion_failures ADD COLUMN frame_id UUID REFERENCES public.t_data_frames(frame_id)';
  EXECUTE 'ALTER TABLE public.t_ingestion_failures DROP CONSTRAINT t_ingestion_failures_stage_check';
  EXECUTE $ddl$
    ALTER TABLE public.t_ingestion_failures
      ADD CONSTRAINT t_ingestion_failures_stage_check CHECK (
        stage IN ('parse','l0','conversion','l2','outbox','frame')
      )
  $ddl$;
  EXECUTE $ddl$
    CREATE UNIQUE INDEX uq_ingestion_failure_frame
      ON public.t_ingestion_failures(frame_id) WHERE frame_id IS NOT NULL
  $ddl$;

  EXECUTE $ddl$
    CREATE TABLE public.t_data_frame_outbox (
      frame_id UUID PRIMARY KEY,
      frame_sequence BIGINT NOT NULL UNIQUE,
      terminal_status TEXT NOT NULL
        CHECK (terminal_status IN ('COMPLETE','FAILED')),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      published_at TIMESTAMPTZ,
      attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
      next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      claimed_by UUID,
      claim_token UUID,
      claimed_until TIMESTAMPTZ,
      CONSTRAINT chk_data_frame_outbox_claim CHECK (
        (claimed_by IS NULL AND claim_token IS NULL AND claimed_until IS NULL)
        OR
        (claimed_by IS NOT NULL AND claim_token IS NOT NULL
          AND claimed_until IS NOT NULL)
      ),
      CONSTRAINT fk_data_frame_outbox_terminal
        FOREIGN KEY (frame_id, frame_sequence, terminal_status)
        REFERENCES public.t_data_frames(frame_id, frame_sequence, status)
        DEFERRABLE INITIALLY DEFERRED
    )
  $ddl$;
  EXECUTE $ddl$
    CREATE INDEX ix_data_frame_outbox_pending
      ON public.t_data_frame_outbox(frame_sequence) WHERE published_at IS NULL
  $ddl$;

  EXECUTE 'DROP TABLE public.t_l2_stream_outbox';
END
$migration$;

CREATE OR REPLACE FUNCTION public.guard_data_frame_transition()
RETURNS TRIGGER LANGUAGE plpgsql AS $function$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'DATA_FRAME_TERMINAL_IMMUTABLE: frame deletion is forbidden'
      USING ERRCODE = '55000';
  END IF;
  IF OLD.status IN ('COMPLETE','FAILED') THEN
    RAISE EXCEPTION 'DATA_FRAME_TERMINAL_IMMUTABLE: terminal frame is immutable'
      USING ERRCODE = '55000';
  END IF;
  IF (NEW.frame_id, NEW.frame_sequence, NEW.candidate_digest, NEW.capture_beat,
      NEW.shot_at, NEW.configuration_revision, NEW.created_at)
     IS DISTINCT FROM
     (OLD.frame_id, OLD.frame_sequence, OLD.candidate_digest, OLD.capture_beat,
      OLD.shot_at, OLD.configuration_revision, OLD.created_at) THEN
    RAISE EXCEPTION 'DATA_FRAME_IDENTITY_IMMUTABLE: frame identity is immutable'
      USING ERRCODE = '55000';
  END IF;
  IF OLD.status = 'PENDING' AND NEW.status = 'PROCESSING'
     AND NEW.attempt_count = OLD.attempt_count + 1 THEN
    RETURN NEW;
  END IF;
  IF OLD.status = 'PROCESSING' AND NEW.status = 'PENDING'
     AND NEW.attempt_count = OLD.attempt_count
     AND NEW.processing_owner IS NULL AND NEW.processing_token IS NULL
     AND NEW.lease_until IS NULL AND NEW.finished_at IS NULL
     AND NEW.failure_code IS NULL THEN
    RETURN NEW;
  END IF;
  IF OLD.status = 'PROCESSING' AND NEW.status = 'PROCESSING'
     AND OLD.lease_until <= clock_timestamp()
     AND NEW.processing_token IS DISTINCT FROM OLD.processing_token
     AND NEW.attempt_count = OLD.attempt_count + 1 THEN
    RETURN NEW;
  END IF;
  IF OLD.status = 'PROCESSING' AND NEW.status IN ('COMPLETE','FAILED')
     AND NEW.attempt_count = OLD.attempt_count THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'DATA_FRAME_TRANSITION_INVALID: % -> %', OLD.status, NEW.status
    USING ERRCODE = '55000';
END
$function$;

DROP TRIGGER IF EXISTS trg_guard_data_frame_transition ON public.t_data_frames;
CREATE TRIGGER trg_guard_data_frame_transition
BEFORE UPDATE OR DELETE ON public.t_data_frames
FOR EACH ROW EXECUTE FUNCTION public.guard_data_frame_transition();

CREATE OR REPLACE FUNCTION public.validate_data_frame_terminal_evidence()
RETURNS TRIGGER LANGUAGE plpgsql AS $function$
DECLARE
  matching_outbox INTEGER;
  matching_failure INTEGER;
BEGIN
  IF NEW.status NOT IN ('COMPLETE','FAILED') THEN
    RETURN NULL;
  END IF;
  SELECT count(*) INTO matching_outbox
  FROM public.t_data_frame_outbox
  WHERE frame_id = NEW.frame_id
    AND frame_sequence = NEW.frame_sequence
    AND terminal_status = NEW.status;
  IF matching_outbox <> 1 THEN
    RAISE EXCEPTION 'DATA_FRAME_OUTBOX_EVIDENCE_INVALID: terminal outbox missing'
      USING ERRCODE = '23514';
  END IF;
  IF NEW.status = 'FAILED' THEN
    SELECT count(*) INTO matching_failure
    FROM public.t_ingestion_failures
    WHERE frame_id = NEW.frame_id AND stage = 'frame'
      AND source_digest = NEW.candidate_digest
      AND safe_summary->>'code' = NEW.failure_code;
    IF matching_failure <> 1 THEN
      RAISE EXCEPTION 'DATA_FRAME_FAILURE_EVIDENCE_INVALID: failure fact missing or mismatched'
        USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NULL;
END
$function$;

DROP TRIGGER IF EXISTS trg_validate_data_frame_terminal_evidence ON public.t_data_frames;
CREATE CONSTRAINT TRIGGER trg_validate_data_frame_terminal_evidence
AFTER INSERT OR UPDATE ON public.t_data_frames
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.validate_data_frame_terminal_evidence();

ALTER TABLE public.t_l2_observations DROP CONSTRAINT chk_l2_typed_value;
ALTER TABLE public.t_l2_observations ADD CONSTRAINT chk_l2_typed_value CHECK (
  (quality = 0 AND num_nonnulls(value_float,value_int,value_numeric,value_bool,value_text,value_codes) = 0)
  OR
  (quality IN (64,192) AND num_nonnulls(value_float,value_int,value_numeric,value_bool,value_text,value_codes) = 1)
  OR
  (quality = 1 AND (
    num_nonnulls(value_float,value_int,value_numeric,value_bool,value_text,value_codes) = 1
    OR (num_nonnulls(value_float,value_int,value_numeric,value_bool,value_text,value_codes) = 0
      AND reason = 'FRAME_PROCESSING_FAILED_NO_BASELINE')
  ))
) NOT VALID;

ALTER TABLE public.t_l2_latest DROP CONSTRAINT chk_l2_latest_typed_value;
ALTER TABLE public.t_l2_latest ADD CONSTRAINT chk_l2_latest_typed_value CHECK (
  (quality = 0 AND num_nonnulls(value_float,value_int,value_numeric,value_bool,value_text,value_codes) = 0)
  OR
  (quality IN (64,192) AND num_nonnulls(value_float,value_int,value_numeric,value_bool,value_text,value_codes) = 1)
  OR
  (quality = 1 AND (
    num_nonnulls(value_float,value_int,value_numeric,value_bool,value_text,value_codes) = 1
    OR (num_nonnulls(value_float,value_int,value_numeric,value_bool,value_text,value_codes) = 0
      AND reason = 'FRAME_PROCESSING_FAILED_NO_BASELINE')
  ))
) NOT VALID;

CREATE OR REPLACE FUNCTION public.validate_l2_typed_value_against_entity()
RETURNS TRIGGER LANGUAGE plpgsql AS $function$
DECLARE
  expected_type TEXT;
  value_count INTEGER;
  value_matches BOOLEAN;
BEGIN
  SELECT data_type INTO expected_type
  FROM public.t_entity_instances WHERE id = NEW.entity_instance_id;
  IF expected_type IS NULL THEN
    RETURN NEW;
  END IF;
  value_count := num_nonnulls(
    NEW.value_float, NEW.value_int, NEW.value_numeric,
    NEW.value_bool, NEW.value_text, NEW.value_codes
  );
  IF NEW.quality = 0 THEN
    value_matches := value_count = 0;
  ELSIF NEW.quality = 1 AND value_count = 0 THEN
    value_matches := NEW.reason = 'FRAME_PROCESSING_FAILED_NO_BASELINE';
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
END
$function$;

CREATE OR REPLACE FUNCTION public.validate_empty_stale_evidence()
RETURNS TRIGGER LANGUAGE plpgsql AS $function$
DECLARE
  sequence_value BIGINT;
  value_count INTEGER;
  failed_frame_count INTEGER;
  earlier_baseline_count INTEGER;
BEGIN
  value_count := num_nonnulls(
    NEW.value_float, NEW.value_int, NEW.value_numeric,
    NEW.value_bool, NEW.value_text, NEW.value_codes
  );
  IF NEW.quality <> 1 OR value_count <> 0 THEN
    RETURN NULL;
  END IF;
  sequence_value := CASE
    WHEN TG_TABLE_NAME = 't_l2_observations' THEN NEW.commit_sequence
    ELSE NEW.frame_sequence
  END;
  SELECT count(*) INTO failed_frame_count
  FROM public.t_data_frames
  WHERE frame_sequence = sequence_value AND status = 'FAILED';
  SELECT count(*) INTO earlier_baseline_count
  FROM public.t_l2_observations
  WHERE entity_instance_id = NEW.entity_instance_id
    AND commit_sequence < sequence_value
    AND quality IN (1,64,192)
    AND num_nonnulls(
      value_float,value_int,value_numeric,value_bool,value_text,value_codes
    ) = 1;
  IF NEW.reason <> 'FRAME_PROCESSING_FAILED_NO_BASELINE'
     OR failed_frame_count <> 1 OR earlier_baseline_count <> 0 THEN
    RAISE EXCEPTION 'L2_EMPTY_STALE_EVIDENCE_INVALID: failed first frame required'
      USING ERRCODE = '23514';
  END IF;
  RETURN NULL;
END
$function$;

DROP TRIGGER IF EXISTS trg_validate_empty_stale_observation ON public.t_l2_observations;
CREATE CONSTRAINT TRIGGER trg_validate_empty_stale_observation
AFTER INSERT OR UPDATE ON public.t_l2_observations
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.validate_empty_stale_evidence();

DROP TRIGGER IF EXISTS trg_validate_empty_stale_latest ON public.t_l2_latest;
CREATE CONSTRAINT TRIGGER trg_validate_empty_stale_latest
AFTER INSERT OR UPDATE ON public.t_l2_latest
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.validate_empty_stale_evidence();

COMMIT;
