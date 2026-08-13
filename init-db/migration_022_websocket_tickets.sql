-- Migration 022: short-lived, single-use WebSocket authentication tickets.
-- Raw tickets never enter PostgreSQL; only their SHA-256 digest is persisted.

BEGIN;

CREATE TABLE IF NOT EXISTS t_auth_ws_tickets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES t_auth_sessions(id) ON DELETE CASCADE,
    token_digest CHAR(64) NOT NULL UNIQUE
        CHECK (token_digest ~ '^[0-9a-f]{64}$'),
    capability TEXT NOT NULL CHECK (length(btrim(capability)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    CHECK (expires_at > created_at),
    CHECK (consumed_at IS NULL OR consumed_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_auth_ws_tickets_expiry
    ON t_auth_ws_tickets(expires_at)
    WHERE consumed_at IS NULL;

COMMENT ON TABLE t_auth_ws_tickets IS
    'Thirty-second single-use WebSocket tickets; only SHA-256 digests are stored';

COMMIT;
