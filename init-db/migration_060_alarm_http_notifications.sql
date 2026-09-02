-- Schema 060: configurable, post-commit alarm HTTP notification delivery.

BEGIN;

DO $migration$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('zizu-schema-060'));
  IF to_regclass('public.t_alarm_definitions') IS NULL
     OR to_regclass('public.t_alarm_transitions') IS NULL
     OR to_regclass('public.t_alarm_notification_outbox') IS NULL THEN
    RAISE EXCEPTION 'SCHEMA_060_REQUIRES_UNIFIED_ALARM_RUNTIME';
  END IF;
END
$migration$;

CREATE TABLE IF NOT EXISTS public.t_alarm_http_notification_configs (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL UNIQUE CHECK (btrim(name) <> ''),
  description TEXT,
  method TEXT NOT NULL CHECK (method IN ('GET','POST','PUT','PATCH','DELETE')),
  encrypted_url TEXT NOT NULL,
  url_display TEXT NOT NULL,
  public_query_params JSONB NOT NULL DEFAULT '[]'::JSONB,
  encrypted_secret_query_params TEXT,
  public_headers JSONB NOT NULL DEFAULT '[]'::JSONB,
  encrypted_secret_headers TEXT,
  content_type TEXT NOT NULL CHECK (btrim(content_type) <> ''),
  body_template TEXT NOT NULL DEFAULT '',
  timeout_seconds INTEGER NOT NULL DEFAULT 5
    CHECK (timeout_seconds BETWEEN 1 AND 30),
  current_digest CHAR(64) NOT NULL
    CHECK (current_digest ~ '^[0-9a-f]{64}$'),
  tested_digest CHAR(64)
    CHECK (tested_digest IS NULL OR tested_digest ~ '^[0-9a-f]{64}$'),
  tested_at TIMESTAMPTZ,
  last_test_status JSONB,
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  created_by TEXT NOT NULL CHECK (btrim(created_by) <> ''),
  updated_by TEXT NOT NULL CHECK (btrim(updated_by) <> ''),
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT chk_alarm_http_notification_test_state CHECK (
    (tested_digest IS NULL AND tested_at IS NULL)
    OR (tested_digest IS NOT NULL AND tested_at IS NOT NULL)
  ),
  CONSTRAINT chk_alarm_http_notification_enable_state CHECK (
    enabled = FALSE OR tested_digest = current_digest
  )
);

CREATE TABLE IF NOT EXISTS public.t_alarm_http_notification_bindings (
  definition_id UUID PRIMARY KEY
    REFERENCES public.t_alarm_definitions(id) ON DELETE CASCADE,
  configuration_id UUID NOT NULL
    REFERENCES public.t_alarm_http_notification_configs(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  created_by TEXT NOT NULL CHECK (btrim(created_by) <> '')
);
CREATE INDEX IF NOT EXISTS ix_alarm_http_notification_bindings_config
  ON public.t_alarm_http_notification_bindings(configuration_id);

ALTER TABLE public.t_alarm_notification_outbox
  ADD COLUMN IF NOT EXISTS transition_id UUID
    REFERENCES public.t_alarm_transitions(id),
  ADD COLUMN IF NOT EXISTS transition_code TEXT,
  ADD COLUMN IF NOT EXISTS configuration_id UUID
    REFERENCES public.t_alarm_http_notification_configs(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS configuration_name_snapshot TEXT,
  ADD COLUMN IF NOT EXISTS context_snapshot JSONB,
  ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cycle_attempt_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  ADD COLUMN IF NOT EXISTS lease_owner TEXT,
  ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_target_display TEXT,
  ADD COLUMN IF NOT EXISTS last_http_status INTEGER,
  ADD COLUMN IF NOT EXISTS last_error_code TEXT,
  ADD COLUMN IF NOT EXISTS last_error_detail TEXT,
  ADD COLUMN IF NOT EXISTS last_response_excerpt TEXT,
  ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp();

UPDATE public.t_alarm_notification_outbox
SET status = CASE WHEN delivered_at IS NULL THEN 'cancelled' ELSE 'delivered' END,
    cancelled_at = CASE WHEN delivered_at IS NULL THEN clock_timestamp() ELSE cancelled_at END,
    last_error_code = CASE
      WHEN delivered_at IS NULL THEN 'LEGACY_NOTIFICATION_NOT_REPLAYED'
      ELSE last_error_code
    END,
    updated_at = clock_timestamp()
WHERE transition_id IS NULL;

DO $constraints$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='chk_alarm_notification_context'
      AND conrelid='public.t_alarm_notification_outbox'::regclass
  ) THEN
    ALTER TABLE public.t_alarm_notification_outbox
      ADD CONSTRAINT chk_alarm_notification_context
      CHECK (transition_id IS NULL OR context_snapshot IS NOT NULL) NOT VALID;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='chk_alarm_notification_status'
      AND conrelid='public.t_alarm_notification_outbox'::regclass
  ) THEN
    ALTER TABLE public.t_alarm_notification_outbox
      ADD CONSTRAINT chk_alarm_notification_status
      CHECK (status IN ('pending','retry_wait','delivered','failed','cancelled'));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='chk_alarm_notification_attempt_counts'
      AND conrelid='public.t_alarm_notification_outbox'::regclass
  ) THEN
    ALTER TABLE public.t_alarm_notification_outbox
      ADD CONSTRAINT chk_alarm_notification_attempt_counts
      CHECK (
        attempt_count >= 0
        AND cycle_attempt_count BETWEEN 0 AND 4
        AND attempt_count >= cycle_attempt_count
      );
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='chk_alarm_notification_transition_code'
      AND conrelid='public.t_alarm_notification_outbox'::regclass
  ) THEN
    ALTER TABLE public.t_alarm_notification_outbox
      ADD CONSTRAINT chk_alarm_notification_transition_code
      CHECK (
        transition_id IS NULL
        OR transition_code IN ('ALARM_ACTIVATED','ALARM_RECOVERED')
      ) NOT VALID;
  END IF;
END
$constraints$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_alarm_notification_transition
  ON public.t_alarm_notification_outbox(transition_id)
  WHERE transition_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_alarm_notification_claim
  ON public.t_alarm_notification_outbox(status,next_attempt_at,lease_expires_at)
  WHERE status IN ('pending','retry_wait');
CREATE INDEX IF NOT EXISTS ix_alarm_notification_event_type
  ON public.t_alarm_notification_outbox(event_id,transition_code,created_at);

CREATE TABLE IF NOT EXISTS public.t_alarm_notification_attempts (
  id UUID PRIMARY KEY,
  notification_id UUID NOT NULL
    REFERENCES public.t_alarm_notification_outbox(id) ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
  attempted_at TIMESTAMPTZ NOT NULL,
  method TEXT NOT NULL
    CHECK (method IN ('GET','POST','PUT','PATCH','DELETE')),
  target_display TEXT NOT NULL,
  duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
  outcome TEXT NOT NULL
    CHECK (outcome IN ('delivered','rejected','timeout','network_error','render_error')),
  http_status INTEGER,
  error_code TEXT,
  error_detail TEXT,
  response_excerpt TEXT,
  UNIQUE(notification_id,attempt_no)
);
CREATE INDEX IF NOT EXISTS ix_alarm_notification_attempts_notification
  ON public.t_alarm_notification_attempts(notification_id,attempt_no);

CREATE TABLE IF NOT EXISTS public.t_alarm_notification_retry_idempotency (
  actor TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  notification_id UUID NOT NULL
    REFERENCES public.t_alarm_notification_outbox(id) ON DELETE CASCADE,
  response JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(actor,idempotency_key)
);

COMMENT ON TABLE public.t_alarm_http_notification_configs IS
  'Administrator-managed HTTP requests for post-commit alarm notifications';
COMMENT ON TABLE public.t_alarm_notification_attempts IS
  'Append-only redacted evidence for each HTTP notification attempt';

COMMIT;
