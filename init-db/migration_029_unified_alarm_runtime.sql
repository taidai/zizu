-- ADR-0004: immutable alarm definitions and unified event lifecycle.
-- Legacy t_alarms remains read-only compatibility history; no rows are guessed
-- or rewritten into this model.
BEGIN;

ALTER TABLE t_solution_install_plans
    ADD COLUMN IF NOT EXISTS alarm_plan JSONB;

CREATE TABLE IF NOT EXISTS t_alarm_definitions (
    id UUID PRIMARY KEY,
    asset_id TEXT NOT NULL CHECK (length(btrim(asset_id)) > 0),
    definition_version TEXT NOT NULL CHECK (length(btrim(definition_version)) > 0),
    installation_id UUID NOT NULL REFERENCES t_solution_installations(id)
        DEFERRABLE INITIALLY DEFERRED,
    site_configuration_version INTEGER NOT NULL,
    entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
    entity_definition_id TEXT NOT NULL CHECK (length(btrim(entity_definition_id)) > 0),
    trigger_condition JSONB NOT NULL,
    trigger_duration_seconds DOUBLE PRECISION NOT NULL CHECK (trigger_duration_seconds > 0),
    recovery_condition JSONB NOT NULL,
    recovery_duration_seconds DOUBLE PRECISION NOT NULL CHECK (recovery_duration_seconds > 0),
    severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'MAJOR', 'CRITICAL')),
    notification_throttle_seconds DOUBLE PRECISION NOT NULL CHECK (notification_throttle_seconds > 0),
    content_digest CHAR(64) NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (installation_id, asset_id, entity_instance_id)
);

-- The current index is mutable deployment projection. Definitions themselves
-- are immutable evidence; historical events continue to reference their exact
-- definition/version after a package upgrade changes this index.
CREATE TABLE IF NOT EXISTS t_alarm_definition_current (
    asset_id TEXT NOT NULL,
    entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
    definition_id UUID NOT NULL REFERENCES t_alarm_definitions(id),
    site_configuration_version INTEGER NOT NULL,
    PRIMARY KEY (asset_id, entity_instance_id),
    UNIQUE (definition_id)
);
CREATE INDEX IF NOT EXISTS idx_alarm_definition_current_entity
    ON t_alarm_definition_current(entity_instance_id);

CREATE TABLE IF NOT EXISTS t_alarm_events (
    id UUID PRIMARY KEY,
    definition_id UUID NOT NULL REFERENCES t_alarm_definitions(id),
    definition_version TEXT NOT NULL,
    entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
    state TEXT NOT NULL CHECK (state IN ('normal', 'pending', 'active_unacknowledged', 'active_acknowledged', 'recovered')),
    severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'MAJOR', 'CRITICAL')),
    pending_at TIMESTAMPTZ NOT NULL,
    active_at TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by TEXT,
    acknowledgement_note TEXT,
    recovery_candidate_since TIMESTAMPTZ,
    recovered_at TIMESTAMPTZ,
    first_observation JSONB,
    last_observation JSONB,
    recovery_observation JSONB
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_alarm_events_one_open
    ON t_alarm_events(definition_id, entity_instance_id)
    WHERE state IN ('pending', 'active_unacknowledged', 'active_acknowledged');
CREATE INDEX IF NOT EXISTS idx_alarm_events_current
    ON t_alarm_events(state, severity, pending_at DESC);

CREATE TABLE IF NOT EXISTS t_alarm_transitions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID NOT NULL REFERENCES t_alarm_events(id),
    audit_event_id UUID NOT NULL REFERENCES t_audit_events(id),
    from_state TEXT,
    to_state TEXT NOT NULL CHECK (to_state IN ('normal', 'pending', 'active_unacknowledged', 'active_acknowledged', 'recovered')),
    occurred_at TIMESTAMPTZ NOT NULL,
    code TEXT NOT NULL,
    evidence JSONB,
    actor TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_alarm_transitions_event
    ON t_alarm_transitions(event_id, occurred_at, id);

CREATE TABLE IF NOT EXISTS t_alarm_notification_outbox (
    id UUID PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES t_alarm_events(id),
    definition_id UUID NOT NULL REFERENCES t_alarm_definitions(id),
    entity_instance_id UUID NOT NULL REFERENCES t_entity_instances(id),
    created_at TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ,
    delivery_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_alarm_notification_outbox_pending
    ON t_alarm_notification_outbox(created_at)
    WHERE delivered_at IS NULL;

CREATE OR REPLACE FUNCTION reject_alarm_transition_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'alarm transitions are append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_alarm_transitions_append_only ON t_alarm_transitions;
CREATE TRIGGER trg_alarm_transitions_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_alarm_transitions
    FOR EACH STATEMENT EXECUTE FUNCTION reject_alarm_transition_mutation();

CREATE OR REPLACE FUNCTION enforce_alarm_event_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state IN ('normal', 'recovered') AND OLD.state <> NEW.state THEN
        RAISE EXCEPTION 'terminal alarm event state cannot change';
    END IF;
    IF OLD.state <> NEW.state AND (OLD.state, NEW.state) NOT IN (
        ('pending', 'active_unacknowledged'),
        ('pending', 'normal'),
        ('active_unacknowledged', 'active_acknowledged'),
        ('active_unacknowledged', 'recovered'),
        ('active_acknowledged', 'recovered')
    ) THEN
        RAISE EXCEPTION 'invalid alarm event state transition: % -> %', OLD.state, NEW.state;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_alarm_event_state_monotonic ON t_alarm_events;
CREATE TRIGGER trg_alarm_event_state_monotonic
    BEFORE UPDATE OF state ON t_alarm_events
    FOR EACH ROW EXECUTE FUNCTION enforce_alarm_event_transition();

COMMENT ON TABLE t_alarm_definitions IS
    'Immutable package-installed alarm definition versions';
COMMENT ON TABLE t_alarm_events IS
    'ADR-0004 lifecycle events; legacy t_alarms is never backfilled';

COMMIT;
