"""生产身份、持久会话与动作授权深模块。"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import secrets
from typing import Callable, Protocol
from uuid import UUID, uuid4


PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000
SESSION_TOKEN_PREFIX = "zizu_s1_"
SESSION_TOKEN_BYTES = 32
LOGIN_MAX_FAILURES = 5
LOGIN_IP_MAX_FAILURES = 25
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_BLOCK = timedelta(minutes=15)
REQUIRED_IDENTITY_TABLES = frozenset(
    {
        "t_users",
        "t_auth_sessions",
        "t_auth_login_limits",
        "t_audit_events",
    }
)


class IdentityError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    username: str
    role: str
    session_id: UUID

    @property
    def actor(self) -> str:
        return f"user:{self.user_id}"


@dataclass(frozen=True)
class UserIdentity:
    id: UUID
    username: str
    password_hash: str
    role: str
    status: str
    auth_version: int = 1


@dataclass(frozen=True)
class Session:
    id: UUID
    user_id: UUID
    token_digest: str
    auth_version: int
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class AuthenticatedSession:
    access_token: str
    expires_at: datetime
    principal: Principal


@dataclass(frozen=True)
class AuditEvent:
    event: str
    outcome: str
    reason: str | None = None
    actor: str | None = None
    target: str | None = None
    request_id: str | None = None
    client_ip: str | None = None
    details: dict[str, object] | None = None


@dataclass(frozen=True)
class LoginSubject:
    subject_type: str
    subject_digest: str


class IdentityRepository(Protocol):
    def find_user(self, username: str) -> UserIdentity | None: ...

    def find_session(self, token_digest: str) -> tuple[Session, UserIdentity] | None: ...

    def login_blocked_until(
        self,
        subjects: tuple[LoginSubject, ...],
        now: datetime,
    ) -> datetime | None: ...

    def record_login_failure(
        self,
        subjects: tuple[LoginSubject, ...],
        now: datetime,
        event: AuditEvent,
    ) -> datetime | None: ...

    def complete_login(
        self,
        session: Session,
        logged_in_at: datetime,
        username_subject: LoginSubject,
        event: AuditEvent,
    ) -> bool: ...

    def revoke_session(
        self,
        session_id: UUID,
        revoked_at: datetime,
        event: AuditEvent,
    ) -> None: ...

    def append_audit(
        self,
        event: AuditEvent,
        *,
        connection: object | None = None,
    ) -> None: ...


def verify_identity_schema(connection_factory: Callable[[], object] | None = None) -> None:
    """Fail when a deployment started without the identity migration."""
    if connection_factory is None:
        from app.services.telemetry_store import get_connection

        connection_factory = get_connection
    with connection_factory() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = ANY(%s)
            """,
            (list(REQUIRED_IDENTITY_TABLES),),
        )
        present = {row[0] for row in cursor.fetchall()}
    missing = REQUIRED_IDENTITY_TABLES - present
    if missing:
        raise RuntimeError(
            "Identity schema is incomplete; missing: " + ", ".join(sorted(missing))
        )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if not password:
        raise ValueError("password must not be empty")
    if len(password.encode("utf-8")) > 1024:
        raise ValueError("password must not exceed 1024 UTF-8 bytes")
    resolved_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        resolved_salt,
        PASSWORD_ITERATIONS,
    )
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(PASSWORD_ITERATIONS),
            _b64encode(resolved_salt),
            _b64encode(digest),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        if len(password.encode("utf-8")) > 1024:
            return False
        scheme, iterations_text, salt_text, expected_text = encoded.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(iterations_text)
        if iterations < 1 or iterations > 2_000_000:
            return False
        salt = _b64decode(salt_text)
        expected = _b64decode(expected_text)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _subject_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _login_subjects(
    username: str | None,
    client_ip: str | None,
) -> tuple[LoginSubject, ...]:
    subjects = []
    if username is not None:
        subjects.append(LoginSubject("username", _subject_digest(username.casefold())))
    if client_ip:
        subjects.append(LoginSubject("client_ip", _subject_digest(client_ip)))
    return tuple(subjects)


_dummy_hash: str | None = None


def _dummy_password_hash() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(
            "not-a-real-user-password",
            salt=b"zizu-dummy-salt!",
        )
    return _dummy_hash


CAPABILITY_ROLES: dict[str, frozenset[str]] = {
    "runtime.read": frozenset({"admin", "engineer", "operator"}),
    "configuration.read": frozenset({"admin", "engineer"}),
    "configuration.write": frozenset({"admin", "engineer"}),
    "alarm.acknowledge": frozenset({"admin", "engineer", "operator"}),
    # Temporary compatibility seam. Ticket #14 removes manual alarm creation
    # and recovery after every source uses the unified alarm state machine.
    "legacy_alarm.write": frozenset({"admin", "engineer"}),
    "solution.package.import": frozenset({"admin"}),
    "solution.package.read": frozenset({"admin"}),
    "solution.install.plan": frozenset({"admin", "engineer"}),
    "solution.install.apply": frozenset({"admin", "engineer"}),
    "solution.installation.read": frozenset({"admin", "engineer", "operator"}),
    "solution.acceptance.run": frozenset({"admin", "engineer"}),
    "solution.report.read": frozenset({"admin", "engineer", "operator"}),
}


class Identity:
    """一个小接口隐藏密码校验、会话撤销、当前角色和审计。"""

    def __init__(
        self,
        repository: IdentityRepository,
        *,
        session_minutes: int = 480,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._session_minutes = session_minutes
        self._now = now or (lambda: datetime.now(timezone.utc))

    def authenticate(
        self,
        username: str,
        password: str,
        *,
        request_id: str | None = None,
        client_ip: str | None = None,
    ) -> AuthenticatedSession:
        normalized = username.strip()
        now = self._now()
        subjects = _login_subjects(normalized, client_ip)
        blocked_until = self._repository.login_blocked_until(subjects, now)
        if blocked_until is not None and blocked_until > now:
            retry_after = max(1, math.ceil((blocked_until - now).total_seconds()))
            self._repository.append_audit(
                AuditEvent(
                    event="identity.login",
                    outcome="denied",
                    reason="login_throttled",
                    request_id=request_id,
                    client_ip=client_ip,
                )
            )
            raise IdentityError(
                "LOGIN_THROTTLED",
                "Too many failed login attempts; retry later",
                status_code=429,
                retry_after_seconds=retry_after,
            )
        user = self._repository.find_user(normalized)
        supported_hash = bool(
            user and user.password_hash.startswith(f"{PASSWORD_SCHEME}$")
        )
        password_hash = user.password_hash if supported_hash else _dummy_password_hash()
        valid = verify_password(password, password_hash)
        if user is None or not valid:
            event = AuditEvent(
                event="identity.login",
                outcome="denied",
                reason="credentials_invalid",
                request_id=request_id,
                client_ip=client_ip,
                details={"username": normalized},
            )
            newly_blocked_until = self._repository.record_login_failure(
                subjects,
                now,
                event,
            )
            if newly_blocked_until is not None and newly_blocked_until > now:
                retry_after = max(
                    1,
                    math.ceil((newly_blocked_until - now).total_seconds()),
                )
                raise IdentityError(
                    "LOGIN_THROTTLED",
                    "Too many failed login attempts; retry later",
                    status_code=429,
                    retry_after_seconds=retry_after,
                )
            raise IdentityError(
                "CREDENTIALS_INVALID",
                "Username or password is invalid",
                status_code=401,
            )
        if user.status == "role_migration_required" or user.role == "viewer":
            self._repository.append_audit(
                AuditEvent(
                    event="identity.login",
                    outcome="denied",
                    reason="role_migration_required",
                    actor=f"user:{user.id}",
                    request_id=request_id,
                    client_ip=client_ip,
                )
            )
            raise IdentityError(
                "ROLE_MIGRATION_REQUIRED",
                "An administrator must assign this account a supported role",
                status_code=403,
            )
        if user.status != "active":
            self._repository.append_audit(
                AuditEvent(
                    event="identity.login",
                    outcome="denied",
                    reason="account_unavailable",
                    actor=f"user:{user.id}",
                    request_id=request_id,
                    client_ip=client_ip,
                )
            )
            raise IdentityError(
                "CREDENTIALS_INVALID",
                "Username or password is invalid",
                status_code=401,
            )

        token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        session = Session(
            id=uuid4(),
            user_id=user.id,
            token_digest=token_digest(token),
            auth_version=user.auth_version,
            created_at=now,
            expires_at=now + timedelta(minutes=self._session_minutes),
        )
        principal = Principal(user.id, user.username, user.role, session.id)
        completed = self._repository.complete_login(
            session,
            now,
            subjects[0],
            AuditEvent(
                event="identity.login",
                outcome="allowed",
                actor=principal.actor,
                request_id=request_id,
                client_ip=client_ip,
            ),
        )
        if not completed:
            self._repository.append_audit(
                AuditEvent(
                    event="identity.login",
                    outcome="denied",
                    reason="account_changed",
                    actor=f"user:{user.id}",
                    request_id=request_id,
                    client_ip=client_ip,
                )
            )
            raise IdentityError(
                "CREDENTIALS_INVALID",
                "Username or password is invalid",
                status_code=401,
            )
        return AuthenticatedSession(token, session.expires_at, principal)

    def reject_login_request(
        self,
        *,
        request_id: str | None = None,
        client_ip: str | None = None,
    ) -> None:
        """Audit and rate-limit a malformed credential request without storing its body."""
        now = self._now()
        subjects = _login_subjects(None, client_ip)
        blocked_until = self._repository.login_blocked_until(subjects, now)
        if blocked_until is not None and blocked_until > now:
            retry_after = max(1, math.ceil((blocked_until - now).total_seconds()))
            self._repository.append_audit(
                AuditEvent(
                    event="identity.login",
                    outcome="denied",
                    reason="login_throttled",
                    request_id=request_id,
                    client_ip=client_ip,
                )
            )
            raise IdentityError(
                "LOGIN_THROTTLED",
                "Too many failed login attempts; retry later",
                status_code=429,
                retry_after_seconds=retry_after,
            )

        event = AuditEvent(
            event="identity.login",
            outcome="denied",
            reason="request_invalid",
            request_id=request_id,
            client_ip=client_ip,
        )
        if not subjects:
            self._repository.append_audit(event)
            return
        newly_blocked_until = self._repository.record_login_failure(
            subjects,
            now,
            event,
        )
        if newly_blocked_until is not None and newly_blocked_until > now:
            retry_after = max(
                1,
                math.ceil((newly_blocked_until - now).total_seconds()),
            )
            raise IdentityError(
                "LOGIN_THROTTLED",
                "Too many failed login attempts; retry later",
                status_code=429,
                retry_after_seconds=retry_after,
            )

    def reject_anonymous(
        self,
        target: str,
        *,
        request_id: str | None = None,
        client_ip: str | None = None,
    ) -> None:
        self._repository.append_audit(
            AuditEvent(
                event="authentication.decision",
                outcome="denied",
                reason="authentication_required",
                target=target,
                request_id=request_id,
                client_ip=client_ip,
            )
        )

    def audit(
        self,
        event: AuditEvent,
        *,
        connection: object | None = None,
    ) -> None:
        """Append an event, optionally inside the caller's business transaction."""
        self._repository.append_audit(event, connection=connection)

    def resolve(
        self,
        token: str,
        *,
        request_id: str | None = None,
        client_ip: str | None = None,
    ) -> Principal:
        try:
            normalized_digest = token_digest(token)
        except (UnicodeEncodeError, AttributeError):
            normalized_digest = None
        if not token.startswith(SESSION_TOKEN_PREFIX) or normalized_digest is None:
            self._reject_token(
                "token_invalid",
                request_id=request_id,
                client_ip=client_ip,
            )
            raise IdentityError("TOKEN_INVALID", "Bearer token is invalid", status_code=401)
        resolved = self._repository.find_session(normalized_digest)
        if resolved is None:
            self._reject_token(
                "token_invalid",
                request_id=request_id,
                client_ip=client_ip,
            )
            raise IdentityError("TOKEN_INVALID", "Bearer token is invalid", status_code=401)
        session, user = resolved
        now = self._now()
        if session.revoked_at is not None:
            self._reject_token(
                "session_revoked",
                actor=f"user:{user.id}",
                request_id=request_id,
                client_ip=client_ip,
            )
            raise IdentityError("SESSION_REVOKED", "Session has been revoked", status_code=401)
        if session.expires_at <= now:
            self._reject_token(
                "token_expired",
                actor=f"user:{user.id}",
                request_id=request_id,
                client_ip=client_ip,
            )
            raise IdentityError("TOKEN_EXPIRED", "Bearer token has expired", status_code=401)
        if user.status != "active" or user.auth_version != session.auth_version:
            self._reject_token(
                "session_revoked",
                actor=f"user:{user.id}",
                request_id=request_id,
                client_ip=client_ip,
            )
            raise IdentityError("SESSION_REVOKED", "Session is no longer valid", status_code=401)
        if user.role not in {"admin", "engineer", "operator"}:
            self._reject_token(
                "session_revoked",
                actor=f"user:{user.id}",
                request_id=request_id,
                client_ip=client_ip,
            )
            raise IdentityError("SESSION_REVOKED", "Session is no longer valid", status_code=401)
        return Principal(user.id, user.username, user.role, session.id)

    def _reject_token(
        self,
        reason: str,
        *,
        actor: str | None = None,
        request_id: str | None = None,
        client_ip: str | None = None,
    ) -> None:
        self._repository.append_audit(
            AuditEvent(
                event="authentication.decision",
                outcome="denied",
                reason=reason,
                actor=actor,
                request_id=request_id,
                client_ip=client_ip,
            )
        )

    def authorize(
        self,
        principal: Principal,
        capability: str,
        *,
        request_id: str | None = None,
        client_ip: str | None = None,
    ) -> Principal:
        allowed_roles = CAPABILITY_ROLES.get(capability)
        if allowed_roles is None:
            raise RuntimeError(f"Unknown capability: {capability}")
        if principal.role not in allowed_roles:
            self._repository.append_audit(
                AuditEvent(
                    event="authorization.decision",
                    outcome="denied",
                    reason="permission_denied",
                    actor=principal.actor,
                    target=capability,
                    request_id=request_id,
                    client_ip=client_ip,
                )
            )
            raise IdentityError(
                "PERMISSION_DENIED",
                "The authenticated identity is not allowed to perform this action",
                status_code=403,
            )
        return principal

    def revoke(self, principal: Principal) -> None:
        now = self._now()
        self._repository.revoke_session(
            principal.session_id,
            now,
            AuditEvent(
                event="identity.logout",
                outcome="allowed",
                actor=principal.actor,
            ),
        )


class InMemoryIdentityRepository:
    def __init__(self, users: list[UserIdentity] | None = None) -> None:
        self.users = {user.username: user for user in users or []}
        self.sessions: dict[str, Session] = {}
        self.audits: list[AuditEvent] = []
        self.login_limits: dict[
            tuple[str, str], tuple[int, datetime, datetime | None]
        ] = {}

    def find_user(self, username: str) -> UserIdentity | None:
        return self.users.get(username)

    def find_session(self, digest: str) -> tuple[Session, UserIdentity] | None:
        session = self.sessions.get(digest)
        if session is None:
            return None
        user = next((item for item in self.users.values() if item.id == session.user_id), None)
        return (session, user) if user else None

    def login_blocked_until(
        self,
        subjects: tuple[LoginSubject, ...],
        now: datetime,
    ) -> datetime | None:
        active = [
            state[2]
            for subject in subjects
            if (state := self.login_limits.get(
                (subject.subject_type, subject.subject_digest)
            ))
            and state[2] is not None
            and state[2] > now
        ]
        return max(active) if active else None

    def record_login_failure(
        self,
        subjects: tuple[LoginSubject, ...],
        now: datetime,
        event: AuditEvent,
    ) -> datetime | None:
        blocked_until: datetime | None = None
        for subject in subjects:
            key = (subject.subject_type, subject.subject_digest)
            state = self.login_limits.get(key)
            if state is None or state[1] + LOGIN_WINDOW <= now:
                count, window_started_at = 1, now
            else:
                count, window_started_at = state[0] + 1, state[1]
            blocked = state[2] if state and state[2] and state[2] > now else None
            threshold = (
                LOGIN_IP_MAX_FAILURES
                if subject.subject_type == "client_ip"
                else LOGIN_MAX_FAILURES
            )
            if count >= threshold:
                blocked = now + LOGIN_BLOCK
            self.login_limits[key] = (count, window_started_at, blocked)
            if blocked and (blocked_until is None or blocked > blocked_until):
                blocked_until = blocked
        self.audits.append(event)
        return blocked_until

    def complete_login(
        self,
        session: Session,
        logged_in_at: datetime,
        username_subject: LoginSubject,
        event: AuditEvent,
    ) -> bool:
        del logged_in_at
        user = next(
            (item for item in self.users.values() if item.id == session.user_id),
            None,
        )
        if (
            user is None
            or user.status != "active"
            or user.auth_version != session.auth_version
        ):
            return False
        self.sessions[session.token_digest] = session
        self.login_limits.pop(
            (username_subject.subject_type, username_subject.subject_digest),
            None,
        )
        self.audits.append(event)
        return True

    def revoke_session(
        self,
        session_id: UUID,
        revoked_at: datetime,
        event: AuditEvent,
    ) -> None:
        for key, session in self.sessions.items():
            if session.id == session_id:
                self.sessions[key] = Session(
                    **{**session.__dict__, "revoked_at": revoked_at}
                )
                break
        self.audits.append(event)

    def append_audit(
        self,
        event: AuditEvent,
        *,
        connection: object | None = None,
    ) -> None:
        self.audits.append(event)


class PostgresIdentityRepository:
    def __init__(self, connection_factory: Callable[[], object] | None = None) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection = connection_factory

    @staticmethod
    def _user(row: tuple[object, ...]) -> UserIdentity:
        return UserIdentity(
            id=row[0],
            username=row[1],
            password_hash=row[2],
            role=row[3],
            status=row[4],
            auth_version=row[5],
        )

    @staticmethod
    def _session(row: tuple[object, ...]) -> Session:
        return Session(
            id=row[0],
            user_id=row[1],
            token_digest=row[2],
            auth_version=row[3],
            created_at=row[4],
            expires_at=row[5],
            revoked_at=row[6],
        )

    def find_user(self, username: str) -> UserIdentity | None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, password_hash, role, status, auth_version
                FROM t_users WHERE username = %s
                """,
                (username,),
            )
            row = cur.fetchone()
            return self._user(row) if row else None

    def find_session(self, digest: str) -> tuple[Session, UserIdentity] | None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.id, s.user_id, s.token_digest, s.auth_version,
                       s.created_at, s.expires_at, s.revoked_at,
                       u.id, u.username, u.password_hash, u.role, u.status,
                       u.auth_version
                FROM t_auth_sessions s
                JOIN t_users u ON u.id = s.user_id
                WHERE s.token_digest = %s
                """,
                (digest,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return self._session(row[:7]), self._user(row[7:])

    @staticmethod
    def _append_audit(cur, event: AuditEvent) -> None:
        cur.execute(
            """
            INSERT INTO t_audit_events
              (id, event, outcome, reason, actor, target, request_id,
               client_ip, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                uuid4(),
                event.event,
                event.outcome,
                event.reason,
                event.actor,
                event.target,
                event.request_id,
                event.client_ip,
                json.dumps(event.details or {}),
            ),
        )

    def login_blocked_until(
        self,
        subjects: tuple[LoginSubject, ...],
        now: datetime,
    ) -> datetime | None:
        active: list[datetime] = []
        with self._connection() as conn, conn.cursor() as cur:
            for subject in subjects:
                cur.execute(
                    """
                    SELECT blocked_until FROM t_auth_login_limits
                    WHERE subject_type = %s AND subject_digest = %s
                    """,
                    (subject.subject_type, subject.subject_digest),
                )
                row = cur.fetchone()
                if row and row[0] is not None and row[0] > now:
                    active.append(row[0])
        return max(active) if active else None

    def record_login_failure(
        self,
        subjects: tuple[LoginSubject, ...],
        now: datetime,
        event: AuditEvent,
    ) -> datetime | None:
        blocked_until: datetime | None = None
        with self._connection() as conn, conn.cursor() as cur:
            for subject in subjects:
                cur.execute(
                    """
                    INSERT INTO t_auth_login_limits
                      (subject_type, subject_digest, failure_count,
                       window_started_at, updated_at)
                    VALUES (%s, %s, 0, %s, %s)
                    ON CONFLICT (subject_type, subject_digest) DO NOTHING
                    """,
                    (
                        subject.subject_type,
                        subject.subject_digest,
                        now,
                        now,
                    ),
                )
                cur.execute(
                    """
                    SELECT failure_count, window_started_at, blocked_until
                    FROM t_auth_login_limits
                    WHERE subject_type = %s AND subject_digest = %s
                    FOR UPDATE
                    """,
                    (subject.subject_type, subject.subject_digest),
                )
                count, window_started_at, existing_block = cur.fetchone()
                if window_started_at + LOGIN_WINDOW <= now:
                    count, window_started_at = 1, now
                else:
                    count += 1
                threshold = (
                    LOGIN_IP_MAX_FAILURES
                    if subject.subject_type == "client_ip"
                    else LOGIN_MAX_FAILURES
                )
                blocked = existing_block if existing_block and existing_block > now else None
                if count >= threshold:
                    blocked = now + LOGIN_BLOCK
                cur.execute(
                    """
                    UPDATE t_auth_login_limits
                    SET failure_count = %s, window_started_at = %s,
                        blocked_until = %s, updated_at = %s
                    WHERE subject_type = %s AND subject_digest = %s
                    """,
                    (
                        count,
                        window_started_at,
                        blocked,
                        now,
                        subject.subject_type,
                        subject.subject_digest,
                    ),
                )
                if blocked and (blocked_until is None or blocked > blocked_until):
                    blocked_until = blocked
            self._append_audit(cur, event)
            conn.commit()
        return blocked_until

    def complete_login(
        self,
        session: Session,
        logged_in_at: datetime,
        username_subject: LoginSubject,
        event: AuditEvent,
    ) -> bool:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, auth_version FROM t_users WHERE id = %s FOR UPDATE",
                (session.user_id,),
            )
            user_state = cur.fetchone()
            if user_state != ("active", session.auth_version):
                return False
            cur.execute(
                """
                INSERT INTO t_auth_sessions
                  (id, user_id, token_digest, auth_version, created_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    session.id,
                    session.user_id,
                    session.token_digest,
                    session.auth_version,
                    session.created_at,
                    session.expires_at,
                ),
            )
            cur.execute(
                "UPDATE t_users SET last_login_at = %s, updated_at = %s WHERE id = %s",
                (logged_in_at, logged_in_at, session.user_id),
            )
            cur.execute(
                """
                DELETE FROM t_auth_login_limits
                WHERE subject_type = %s AND subject_digest = %s
                """,
                (
                    username_subject.subject_type,
                    username_subject.subject_digest,
                ),
            )
            self._append_audit(cur, event)
            conn.commit()
            return True

    def revoke_session(
        self,
        session_id: UUID,
        revoked_at: datetime,
        event: AuditEvent,
    ) -> None:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE t_auth_sessions SET revoked_at = %s "
                "WHERE id = %s AND revoked_at IS NULL",
                (revoked_at, session_id),
            )
            self._append_audit(cur, event)
            conn.commit()

    def append_audit(
        self,
        event: AuditEvent,
        *,
        connection: object | None = None,
    ) -> None:
        if connection is not None:
            with connection.cursor() as cur:
                self._append_audit(cur, event)
            return
        with self._connection() as conn, conn.cursor() as cur:
            self._append_audit(cur, event)
            conn.commit()
