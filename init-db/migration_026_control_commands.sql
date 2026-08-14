-- ADR-0007: persistent control command state, idempotency, cooldown and confirmation.
BEGIN;

ALTER TABLE t_entity_instances
    ADD COLUMN IF NOT EXISTS control_policy JSONB;

CREATE TABLE IF NOT EXISTS t_control_commands (
    id UUID PRIMARY KEY,
    actor TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('manual', 'rule', 'policy', 'compatibility', 'acceptance')),
    capability TEXT NOT NULL CHECK (capability = 'control.write'),
    -- Rejected requests also need immutable evidence. Runtime validation is
    -- the authority for a usable target, so an unknown UUID must not make its
    -- rejection disappear behind a foreign-key error.
    entity_instance_id UUID NOT NULL,
    expected_value JSONB NOT NULL,
    data_type TEXT NOT NULL CHECK (data_type IN ('FLOAT', 'INT', 'BOOL', 'STRING', 'ENUM')),
    tolerance DOUBLE PRECISION,
    policy_snapshot JSONB NOT NULL,
    timeout_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'validated', 'dispatched', 'readback_confirmed', 'rejected', 'timeout', 'failed', 'mismatch')),
    code TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest CHAR(64) NOT NULL,
    audit_event_id UUID NOT NULL REFERENCES t_audit_events(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS t_control_command_idempotency (
    actor TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest CHAR(64) NOT NULL,
    command_id UUID NOT NULL REFERENCES t_control_commands(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (actor, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_control_commands_inflight
ON t_control_commands(status, timeout_at)
WHERE status IN ('accepted', 'validated', 'dispatched');

CREATE TABLE IF NOT EXISTS t_control_command_events (
    id UUID PRIMARY KEY,
    command_id UUID NOT NULL REFERENCES t_control_commands(id),
    audit_event_id UUID NOT NULL REFERENCES t_audit_events(id),
    to_status TEXT NOT NULL CHECK (to_status IN ('accepted', 'validated', 'dispatched', 'readback_confirmed', 'rejected', 'timeout', 'failed', 'mismatch')),
    code TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_control_command_events_command
ON t_control_command_events(command_id, occurred_at, id);

CREATE TABLE IF NOT EXISTS t_control_command_cooldowns (
    entity_instance_id UUID PRIMARY KEY REFERENCES t_entity_instances(id),
    command_id UUID NOT NULL REFERENCES t_control_commands(id),
    until_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS t_control_confirmations (
    id UUID PRIMARY KEY,
    actor TEXT NOT NULL,
    request_digest CHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);

CREATE OR REPLACE FUNCTION reject_control_command_history_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'control command history is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_control_command_events_append_only
ON t_control_command_events;
CREATE TRIGGER trg_control_command_events_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON t_control_command_events
FOR EACH STATEMENT EXECUTE FUNCTION reject_control_command_history_mutation();

CREATE OR REPLACE FUNCTION enforce_control_command_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('readback_confirmed', 'rejected', 'timeout', 'failed', 'mismatch') THEN
        RAISE EXCEPTION 'terminal control command state cannot change';
    END IF;
    IF (OLD.status, NEW.status) NOT IN (
        ('accepted', 'validated'),
        ('validated', 'dispatched'),
        ('validated', 'rejected'),
        ('accepted', 'failed'),
        ('validated', 'failed'),
        ('dispatched', 'readback_confirmed'),
        ('dispatched', 'timeout'),
        ('dispatched', 'failed'),
        ('dispatched', 'mismatch')
    ) THEN
        RAISE EXCEPTION 'invalid control command state transition: % -> %', OLD.status, NEW.status;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_control_command_state_monotonic
ON t_control_commands;
CREATE TRIGGER trg_control_command_state_monotonic
BEFORE UPDATE OF status ON t_control_commands
FOR EACH ROW EXECUTE FUNCTION enforce_control_command_transition();

COMMIT;
