-- Migration 021: 生产身份、持久会话、登录限流与统一追加式审计
--
-- 这是 expand 迁移：保留旧 viewer 行用于人工角色迁移，但 viewer 无法登录。
-- 文件可在 migration_020-only 的测试库或空 schema 上独立执行。

BEGIN;

CREATE TABLE IF NOT EXISTS t_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    auth_version BIGINT NOT NULL DEFAULT 1,
    last_login_at TIMESTAMPTZ,
    password_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Expand the v0.4.77 table without assuming 001-schema.sql was executed.
ALTER TABLE t_users ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE t_users ADD COLUMN IF NOT EXISTS auth_version BIGINT;
ALTER TABLE t_users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;
ALTER TABLE t_users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;
ALTER TABLE t_users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

ALTER TABLE t_users DROP CONSTRAINT IF EXISTS t_users_role_check;
ALTER TABLE t_users DROP CONSTRAINT IF EXISTS t_users_status_check;
ALTER TABLE t_users DROP CONSTRAINT IF EXISTS t_users_auth_version_check;
ALTER TABLE t_users DROP CONSTRAINT IF EXISTS t_users_role_status_check;

UPDATE t_users SET role = 'viewer' WHERE role IS NULL;
UPDATE t_users SET status = 'active' WHERE status IS NULL;
UPDATE t_users SET auth_version = 1 WHERE auth_version IS NULL OR auth_version < 1;
UPDATE t_users SET password_changed_at = COALESCE(created_at, now())
WHERE password_changed_at IS NULL;
UPDATE t_users SET updated_at = COALESCE(created_at, now())
WHERE updated_at IS NULL;

-- viewer is a legacy state, never an implicit operator. A platform administrator
-- must explicitly choose engineer/operator/admin before the account can log in.
UPDATE t_users
SET status = 'role_migration_required', updated_at = now()
WHERE role = 'viewer' AND status IS DISTINCT FROM 'role_migration_required';

-- 001-schema.sql historically inserted an unusable placeholder administrator.
-- Keep the row for deterministic one-time activation, but fail closed meanwhile.
UPDATE t_users
SET status = 'disabled',
    auth_version = auth_version + 1,
    updated_at = now()
WHERE lower(password_hash) LIKE '%placeholder%'
  AND status IS DISTINCT FROM 'disabled';

-- v0.4.77 declared bcrypt support but its production images did not ship a
-- reliable verifier. Never leave such rows looking usable: an administrator
-- can explicitly reactivate one through the offline bootstrap/reset flow.
UPDATE t_users
SET status = 'disabled',
    auth_version = auth_version + 1,
    updated_at = now()
WHERE password_hash NOT LIKE 'pbkdf2_sha256$%'
  AND role <> 'viewer'
  AND status IS DISTINCT FROM 'disabled';

ALTER TABLE t_users ALTER COLUMN role SET NOT NULL;
ALTER TABLE t_users ALTER COLUMN role DROP DEFAULT;
ALTER TABLE t_users ALTER COLUMN status SET DEFAULT 'active';
ALTER TABLE t_users ALTER COLUMN status SET NOT NULL;
ALTER TABLE t_users ALTER COLUMN auth_version SET DEFAULT 1;
ALTER TABLE t_users ALTER COLUMN auth_version SET NOT NULL;
ALTER TABLE t_users ALTER COLUMN password_changed_at SET DEFAULT now();
ALTER TABLE t_users ALTER COLUMN password_changed_at SET NOT NULL;
ALTER TABLE t_users ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE t_users ALTER COLUMN updated_at SET NOT NULL;

ALTER TABLE t_users ADD CONSTRAINT t_users_role_check
    CHECK (role IN ('admin', 'engineer', 'operator', 'viewer'));
ALTER TABLE t_users ADD CONSTRAINT t_users_status_check
    CHECK (status IN ('active', 'disabled', 'role_migration_required'));
ALTER TABLE t_users ADD CONSTRAINT t_users_auth_version_check
    CHECK (auth_version > 0);
ALTER TABLE t_users ADD CONSTRAINT t_users_role_status_check
    CHECK (
        (role = 'viewer' AND status = 'role_migration_required')
        OR
        (role <> 'viewer' AND status <> 'role_migration_required')
    );

CREATE TABLE IF NOT EXISTS t_auth_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES t_users(id) ON DELETE CASCADE,
    token_digest CHAR(64) NOT NULL UNIQUE,
    auth_version BIGINT NOT NULL CHECK (auth_version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    CHECK (token_digest ~ '^[0-9a-f]{64}$'),
    CHECK (expires_at > created_at),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_active_user
    ON t_auth_sessions(user_id, expires_at)
    WHERE revoked_at IS NULL;

-- subject_digest is SHA-256(normalized username or canonical client IP). Raw
-- passwords and bearer tokens are never accepted by this table.
CREATE TABLE IF NOT EXISTS t_auth_login_limits (
    subject_type TEXT NOT NULL
        CHECK (subject_type IN ('username', 'client_ip')),
    subject_digest CHAR(64) NOT NULL
        CHECK (subject_digest ~ '^[0-9a-f]{64}$'),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    blocked_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (subject_type, subject_digest)
);

CREATE INDEX IF NOT EXISTS idx_auth_login_limits_blocked
    ON t_auth_login_limits(blocked_until)
    WHERE blocked_until IS NOT NULL;

-- One append-only audit stream is shared by identity, delivery, control and alarm
-- modules. It must never be added to an administrative truncate whitelist.
CREATE TABLE IF NOT EXISTS t_audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event TEXT NOT NULL CHECK (length(btrim(event)) > 0),
    outcome TEXT NOT NULL CHECK (length(btrim(outcome)) > 0),
    reason TEXT,
    actor TEXT,
    target TEXT,
    request_id TEXT,
    client_ip INET,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_events_created_at
    ON t_audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_actor_created_at
    ON t_audit_events(actor, created_at DESC)
    WHERE actor IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_events_event_created_at
    ON t_audit_events(event, created_at DESC);

CREATE OR REPLACE FUNCTION reject_audit_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 't_audit_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_events_append_only ON t_audit_events;
CREATE TRIGGER trg_audit_events_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON t_audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_event_mutation();

COMMENT ON TABLE t_users IS
    'ZiZu identities; legacy viewer rows require explicit role migration';
COMMENT ON COLUMN t_users.auth_version IS
    'Incrementing this value invalidates every older persistent session';
COMMENT ON TABLE t_auth_sessions IS
    'Persistent revocable sessions; only a SHA-256 token digest is stored';
COMMENT ON TABLE t_auth_login_limits IS
    'Persistent account/client login throttling state keyed by SHA-256 digest';
COMMENT ON TABLE t_audit_events IS
    'Unified immutable audit events for identity, delivery, control and alarms';

COMMIT;
