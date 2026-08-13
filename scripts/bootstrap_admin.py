#!/usr/bin/env python3
"""离线创建/修复 ZiZu 身份；默认只允许引导首个平台管理员。"""
from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path
from typing import Protocol, TextIO


REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"
APP_ROOT = BACKEND_ROOT if BACKEND_ROOT.exists() else REPO_ROOT
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.services.identity import hash_password


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
MINIMUM_PASSWORD_LENGTH = 14


class BootstrapAdminError(RuntimeError):
    """可安全呈现在终端的管理员引导错误。"""


class AdminStore(Protocol):
    def provision(
        self,
        username: str,
        password_hash: str,
        role: str,
        *,
        bootstrap_admin: bool,
    ) -> str: ...


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析不含 Secret 的参数；明确拒绝会进入进程列表的密码参数。"""
    resolved = list(sys.argv[1:] if argv is None else argv)
    if any(item == "--password" or item.startswith("--password=") for item in resolved):
        raise BootstrapAdminError(
            "Administrator password is never accepted as a CLI argument; "
            "use an interactive prompt or --password-stdin."
        )

    parser = argparse.ArgumentParser(description=__doc__, exit_on_error=False)
    parser.add_argument("--username", default="admin")
    parser.add_argument(
        "--role",
        choices=("admin", "engineer", "operator"),
        default="admin",
        help="Role for --provision-user; bootstrap mode only accepts admin.",
    )
    parser.add_argument(
        "--provision-user",
        action="store_true",
        help="Provision or explicitly migrate a user after an admin exists.",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read one password line from standard input without echoing it.",
    )
    try:
        args, unknown = parser.parse_known_args(resolved)
    except argparse.ArgumentError as exc:
        raise BootstrapAdminError("Invalid bootstrap arguments; use --help.") from exc
    if unknown:
        raise BootstrapAdminError("Invalid bootstrap arguments; use --help.")
    return args


def read_password(
    *,
    password_stdin: bool,
    stdin: TextIO | None = None,
) -> str:
    if password_stdin:
        stream = stdin or sys.stdin
        password = stream.readline().rstrip("\r\n")
        if not password:
            raise BootstrapAdminError("No administrator password was received on stdin.")
        return password

    first = getpass.getpass("New administrator password: ")
    second = getpass.getpass("Confirm administrator password: ")
    if first != second:
        raise BootstrapAdminError("Administrator password confirmation did not match.")
    return first


def provision_identity(
    store: AdminStore,
    username: str,
    password: str,
    *,
    role: str = "admin",
    bootstrap_admin: bool = True,
) -> str:
    normalized_username = username.strip()
    if not USERNAME_PATTERN.fullmatch(normalized_username):
        raise BootstrapAdminError(
            "Invalid username; use 3-64 letters, digits, '.', '_' or '-'."
        )
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        raise BootstrapAdminError(
            f"Identity password must contain at least {MINIMUM_PASSWORD_LENGTH} characters."
        )
    if not password.strip():
        raise BootstrapAdminError("Identity password must not be blank.")
    try:
        password_hash = hash_password(password)
    except ValueError as exc:
        raise BootstrapAdminError("Identity password does not satisfy policy.") from exc
    return store.provision(
        normalized_username,
        password_hash,
        role,
        bootstrap_admin=bootstrap_admin,
    )


def bootstrap_admin(store: AdminStore, username: str, password: str) -> str:
    return provision_identity(store, username, password)


class PostgresAdminStore:
    """在同一事务内串行化首个管理员的幂等引导。"""

    def __init__(self, connection_factory=None) -> None:
        self._connection_factory = connection_factory or self._connect

    @staticmethod
    def _connect():
        import psycopg2
        from app.core.config import settings

        return psycopg2.connect(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
        )

    def provision(
        self,
        username: str,
        password_hash: str,
        role: str,
        *,
        bootstrap_admin: bool,
    ) -> str:
        connection = self._connection_factory()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        ("zizu.bootstrap_admin",),
                    )
                    cursor.execute(
                        """
                        SELECT id, username, role, status
                        FROM t_users
                        WHERE role = 'admin' AND status = 'active'
                        ORDER BY created_at, id
                        FOR UPDATE
                        """
                    )
                    active_admins = cursor.fetchall()
                    if bootstrap_admin and role != "admin":
                        raise BootstrapAdminError(
                            "Bootstrap mode only creates an administrator."
                        )
                    same_active = next(
                        (row for row in active_admins if row[1] == username),
                        None,
                    )
                    if same_active is not None:
                        if not bootstrap_admin and role != "admin":
                            raise BootstrapAdminError(
                                "The offline flow cannot demote an active administrator."
                            )
                        cursor.execute(
                            "SELECT password_hash FROM t_users WHERE id = %s",
                            (same_active[0],),
                        )
                        current_hash = cursor.fetchone()[0]
                        if bootstrap_admin and current_hash.startswith("pbkdf2_sha256$"):
                            self._append_audit(cursor, username, "already_active")
                            return "already_active"
                        cursor.execute(
                            """
                            UPDATE t_users
                            SET password_hash = %s,
                                auth_version = auth_version + 1,
                                password_changed_at = now(),
                                updated_at = now()
                            WHERE id = %s
                            """,
                            (password_hash, same_active[0]),
                        )
                        outcome = (
                            "password_reset"
                            if current_hash.startswith("pbkdf2_sha256$")
                            else "password_migrated"
                        )
                        self._append_audit(cursor, username, outcome)
                        return outcome
                    if bootstrap_admin and active_admins:
                        raise BootstrapAdminError(
                            "An active administrator already exists; use the authenticated "
                            "user-management flow."
                        )

                    cursor.execute(
                        "SELECT id, role, status FROM t_users "
                        "WHERE username = %s FOR UPDATE",
                        (username,),
                    )
                    existing = cursor.fetchone()
                    if not bootstrap_admin and not active_admins:
                        raise BootstrapAdminError(
                            "Provisioning users requires an existing active administrator."
                        )
                    if existing is None:
                        cursor.execute(
                            """
                            INSERT INTO t_users
                              (username, password_hash, role, status,
                               auth_version, password_changed_at, updated_at)
                            VALUES (%s, %s, %s, 'active', 1, now(), now())
                            """,
                            (username, password_hash, role),
                        )
                        outcome = "created"
                    else:
                        cursor.execute(
                            """
                            UPDATE t_users
                            SET password_hash = %s,
                                role = %s,
                                status = 'active',
                                auth_version = auth_version + 1,
                                password_changed_at = now(),
                                updated_at = now()
                            WHERE id = %s
                            """,
                            (password_hash, role, existing[0]),
                        )
                        outcome = (
                            "role_migrated"
                            if existing[1] == "viewer"
                            or existing[2] == "role_migration_required"
                            else "activated"
                        )
                    self._append_audit(cursor, username, outcome, role)
                    return outcome
        finally:
            connection.close()

    @staticmethod
    def _append_audit(
        cursor,
        username: str,
        outcome: str,
        role: str = "admin",
    ) -> None:
        cursor.execute(
            """
            INSERT INTO t_audit_events
              (event, outcome, actor, target, details)
            VALUES ('identity.provision', 'allowed', 'system:bootstrap', %s,
                    jsonb_build_object('result', %s, 'role', %s))
            """,
            (f"user:{username}", outcome, role),
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    password = read_password(password_stdin=args.password_stdin)
    result = provision_identity(
        PostgresAdminStore(),
        args.username,
        password,
        role=args.role,
        bootstrap_admin=not args.provision_user,
    )
    messages = {
        "created": "Administrator account created.",
        "activated": "Administrator account activated.",
        "already_active": "Administrator account is already active; no changes made.",
        "password_migrated": "Administrator password migrated to the supported scheme.",
        "password_reset": "Administrator password reset and existing sessions invalidated.",
        "role_migrated": "Legacy account role migrated and activated.",
    }
    print(messages.get(result, "Administrator bootstrap completed."))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapAdminError as exc:
        print(f"Bootstrap refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
