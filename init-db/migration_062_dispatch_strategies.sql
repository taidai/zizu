-- Schema 062: versioned dispatch strategies, ownership, durable intents and events.
BEGIN;

SELECT pg_advisory_xact_lock(hashtext('zizu-schema-062'));

DO $requirements$
BEGIN
  IF to_regclass('public.t_entity_instances') IS NULL
     OR to_regclass('public.t_configuration_revisions') IS NULL
     OR to_regclass('public.t_configuration_state') IS NULL
     OR to_regclass('public.t_control_commands') IS NULL THEN
    RAISE EXCEPTION
      'SCHEMA_062_REQUIRES_061: L2, configuration or control contract is missing'
      USING ERRCODE = '55000';
  END IF;
END
$requirements$;

CREATE TABLE IF NOT EXISTS public.t_dispatch_strategies (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL CONSTRAINT chk_dispatch_strategy_name
    CHECK (btrim(name) <> ''),
  description TEXT,
  active_revision_id UUID,
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  runtime_health TEXT NOT NULL DEFAULT 'READY'
    CONSTRAINT chk_dispatch_strategy_health
    CHECK (runtime_health IN ('READY','BLOCKED','FAILED')),
  last_trigger_key TEXT,
  last_evaluated_at TIMESTAMPTZ,
  last_desired JSONB,
  last_actual JSONB,
  last_evidence JSONB,
  failure_code TEXT,
  created_by TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT chk_dispatch_strategy_enabled_revision
    CHECK (NOT enabled OR active_revision_id IS NOT NULL),
  CONSTRAINT chk_dispatch_strategy_failure
    CHECK (
      (runtime_health = 'FAILED' AND failure_code IS NOT NULL)
      OR (runtime_health <> 'FAILED' AND failure_code IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS public.t_dispatch_strategy_revisions (
  id UUID PRIMARY KEY,
  strategy_id UUID NOT NULL
    REFERENCES public.t_dispatch_strategies(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL CONSTRAINT chk_dispatch_revision_positive
    CHECK (revision > 0),
  lifecycle TEXT NOT NULL CONSTRAINT chk_dispatch_revision_lifecycle
    CHECK (lifecycle IN ('DRAFT','PUBLISHED')),
  trigger_kind TEXT NOT NULL CONSTRAINT chk_dispatch_revision_trigger
    CHECK (trigger_kind IN ('DATA_CHANGE','FIXED_TICK')),
  site_timezone TEXT NOT NULL CONSTRAINT chk_dispatch_revision_timezone
    CHECK (btrim(site_timezone) <> ''),
  jdm_content JSONB NOT NULL CONSTRAINT chk_dispatch_revision_jdm_object
    CHECK (jsonb_typeof(jdm_content) = 'object'),
  content_digest CHAR(64) NOT NULL CONSTRAINT chk_dispatch_revision_digest
    CHECK (content_digest ~ '^[0-9a-f]{64}$'),
  base_configuration_revision BIGINT NOT NULL
    REFERENCES public.t_configuration_revisions(revision),
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  published_by TEXT,
  published_at TIMESTAMPTZ,
  CONSTRAINT uq_dispatch_revision_number UNIQUE(strategy_id,revision),
  CONSTRAINT uq_dispatch_revision_identity UNIQUE(strategy_id,id),
  CONSTRAINT chk_dispatch_revision_publish_evidence CHECK (
    (lifecycle='DRAFT' AND published_by IS NULL AND published_at IS NULL)
    OR (lifecycle='PUBLISHED' AND published_by IS NOT NULL AND published_at IS NOT NULL)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dispatch_strategy_draft
  ON public.t_dispatch_strategy_revisions(strategy_id)
  WHERE lifecycle='DRAFT';

CREATE OR REPLACE FUNCTION public.reject_published_dispatch_revision_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.lifecycle='PUBLISHED' THEN
    RAISE EXCEPTION 'published dispatch strategy revisions are immutable';
  END IF;
  IF TG_OP='DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_dispatch_revision_immutable
  ON public.t_dispatch_strategy_revisions;
CREATE TRIGGER trg_dispatch_revision_immutable
  BEFORE UPDATE OR DELETE ON public.t_dispatch_strategy_revisions
  FOR EACH ROW EXECUTE FUNCTION public.reject_published_dispatch_revision_mutation();

ALTER TABLE public.t_dispatch_strategies
  DROP CONSTRAINT IF EXISTS fk_dispatch_strategy_active_revision;
ALTER TABLE public.t_dispatch_strategies
  ADD CONSTRAINT fk_dispatch_strategy_active_revision
  FOREIGN KEY(id,active_revision_id)
  REFERENCES public.t_dispatch_strategy_revisions(strategy_id,id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS public.t_dispatch_strategy_bindings (
  revision_id UUID NOT NULL
    REFERENCES public.t_dispatch_strategy_revisions(id) ON DELETE CASCADE,
  direction TEXT NOT NULL CONSTRAINT chk_dispatch_binding_direction
    CHECK (direction IN ('INPUT','OUTPUT')),
  binding_key TEXT NOT NULL CONSTRAINT chk_dispatch_binding_key
    CHECK (btrim(binding_key) <> ''),
  ordinal INTEGER NOT NULL CONSTRAINT chk_dispatch_binding_ordinal
    CHECK (ordinal >= 0),
  entity_instance_id UUID NOT NULL REFERENCES public.t_entity_instances(id),
  expected_data_type TEXT NOT NULL CONSTRAINT chk_dispatch_binding_data_type
    CHECK (expected_data_type IN ('FLOAT','INT','BOOL','STRING','ENUM','CODE_SET')),
  unit TEXT,
  freshness_seconds DOUBLE PRECISION NOT NULL
    CONSTRAINT chk_dispatch_binding_freshness CHECK (freshness_seconds > 0),
  PRIMARY KEY(revision_id,direction,binding_key),
  CONSTRAINT uq_dispatch_binding_ordinal UNIQUE(revision_id,direction,ordinal)
);

CREATE INDEX IF NOT EXISTS ix_dispatch_binding_entity
  ON public.t_dispatch_strategy_bindings(entity_instance_id,direction,revision_id);

CREATE TABLE IF NOT EXISTS public.t_dispatch_strategy_owners (
  entity_instance_id UUID PRIMARY KEY REFERENCES public.t_entity_instances(id),
  strategy_id UUID NOT NULL REFERENCES public.t_dispatch_strategies(id),
  revision_id UUID NOT NULL,
  acquired_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT fk_dispatch_owner_revision
    FOREIGN KEY(strategy_id,revision_id)
    REFERENCES public.t_dispatch_strategy_revisions(strategy_id,id)
);

CREATE TABLE IF NOT EXISTS public.t_dispatch_control_intents (
  id UUID PRIMARY KEY,
  strategy_id UUID NOT NULL REFERENCES public.t_dispatch_strategies(id),
  revision_id UUID NOT NULL,
  evaluation_key TEXT NOT NULL CONSTRAINT chk_dispatch_intent_evaluation_key
    CHECK (btrim(evaluation_key) <> ''),
  action_id TEXT NOT NULL CONSTRAINT chk_dispatch_intent_action_id
    CHECK (btrim(action_id) <> ''),
  ordinal INTEGER NOT NULL CONSTRAINT chk_dispatch_intent_ordinal
    CHECK (ordinal >= 0),
  entity_instance_id UUID NOT NULL REFERENCES public.t_entity_instances(id),
  operation TEXT NOT NULL DEFAULT 'SET' CONSTRAINT chk_dispatch_intent_operation
    CHECK (operation = 'SET'),
  expected_value JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING'
    CONSTRAINT chk_dispatch_intent_status
    CHECK (status IN ('PENDING','IN_FLIGHT','CONFIRMED','CANCELLED','FAILED')),
  attempt_count INTEGER NOT NULL DEFAULT 0
    CONSTRAINT chk_dispatch_intent_attempts CHECK (attempt_count BETWEEN 0 AND 3),
  control_command_id UUID REFERENCES public.t_control_commands(id),
  snapshot_evidence JSONB NOT NULL,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  last_error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  dispatched_at TIMESTAMPTZ,
  confirmed_at TIMESTAMPTZ,
  CONSTRAINT fk_dispatch_intent_revision
    FOREIGN KEY(strategy_id,revision_id)
    REFERENCES public.t_dispatch_strategy_revisions(strategy_id,id),
  CONSTRAINT uq_dispatch_intent_evaluation_action
    UNIQUE(revision_id,evaluation_key,action_id),
  CONSTRAINT chk_dispatch_intent_terminal_time CHECK (
    (status='CONFIRMED' AND confirmed_at IS NOT NULL)
    OR (status<>'CONFIRMED' AND confirmed_at IS NULL)
  )
);

CREATE INDEX IF NOT EXISTS ix_dispatch_intent_ready
  ON public.t_dispatch_control_intents(status,next_attempt_at,created_at)
  WHERE status IN ('PENDING','IN_FLIGHT');
CREATE INDEX IF NOT EXISTS ix_dispatch_intent_sequence
  ON public.t_dispatch_control_intents(revision_id,evaluation_key,ordinal);

CREATE TABLE IF NOT EXISTS public.t_dispatch_strategy_events (
  occurred_at TIMESTAMPTZ NOT NULL,
  id UUID NOT NULL,
  strategy_id UUID NOT NULL REFERENCES public.t_dispatch_strategies(id),
  revision_id UUID NOT NULL,
  event_kind TEXT NOT NULL CONSTRAINT chk_dispatch_event_kind
    CHECK (event_kind IN (
      'DECISION_CHANGED','INTENT_CREATED','BLOCKED',
      'BLOCK_REASON_CHANGED','RECOVERED','FAILED'
    )),
  trigger_kind TEXT NOT NULL CONSTRAINT chk_dispatch_event_trigger
    CHECK (trigger_kind IN ('DATA_CHANGE','FIXED_TICK','CONTROL_RESULT')),
  trigger_key TEXT NOT NULL,
  frame_sequence BIGINT,
  configuration_revision BIGINT NOT NULL
    REFERENCES public.t_configuration_revisions(revision),
  snapshot_evidence JSONB NOT NULL,
  decision JSONB,
  intent_summary JSONB,
  control_command_id UUID REFERENCES public.t_control_commands(id),
  reason_code TEXT,
  PRIMARY KEY(occurred_at,id),
  CONSTRAINT fk_dispatch_event_revision
    FOREIGN KEY(strategy_id,revision_id)
    REFERENCES public.t_dispatch_strategy_revisions(strategy_id,id),
  CONSTRAINT chk_dispatch_event_frame_sequence
    CHECK (frame_sequence IS NULL OR frame_sequence > 0)
);

SELECT create_hypertable(
  'public.t_dispatch_strategy_events',
  'occurred_at',
  if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS ix_dispatch_events_strategy_time
  ON public.t_dispatch_strategy_events(strategy_id,occurred_at DESC,id);

CREATE OR REPLACE FUNCTION public.reject_dispatch_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'dispatch strategy events are append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_dispatch_events_append_only
  ON public.t_dispatch_strategy_events;
CREATE TRIGGER trg_dispatch_events_append_only
  BEFORE UPDATE OR DELETE ON public.t_dispatch_strategy_events
  FOR EACH ROW EXECUTE FUNCTION public.reject_dispatch_event_mutation();

ALTER TABLE public.t_control_commands
  DROP CONSTRAINT IF EXISTS t_control_commands_source_type_check;
ALTER TABLE public.t_control_commands
  DROP CONSTRAINT IF EXISTS chk_control_command_source_type;
ALTER TABLE public.t_control_commands
  ADD CONSTRAINT chk_control_command_source_type CHECK (
    source_type IN (
      'manual','rule','policy','compatibility','acceptance','strategy'
    )
  );

COMMIT;
