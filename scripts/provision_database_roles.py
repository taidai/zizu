"""Provision or rotate ZiZu's least-privilege PostgreSQL application role.

Run this as a controlled deployment job, never inside the web backend.  It
needs the schema-owner connection in DB_OWNER_* and reads the web role from
DB_USER/DB_PASSWORD.  It deliberately grants broad existing application
tables, then makes legacy ``t_alarms`` SELECT-only.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import psycopg2
from psycopg2 import sql


ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
MIGRATION_NAME = re.compile(r"^migration_(\d+).*\.sql$")
REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"


def _file_environment() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _dotenv_value(value)
    return values


def _dotenv_value(raw_value: str) -> str:
    """Read the useful subset of dotenv without corrupting a quoted ``#``."""
    value = raw_value.strip()
    quote: str | None = None
    for index, character in enumerate(value):
        if character in {"'", '"'}:
            quote = None if quote == character else (character if quote is None else quote)
        elif character == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            value = value[:index].rstrip()
            break
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value.strip()


FILE_ENVIRONMENT = _file_environment()


def required(name: str) -> str:
    value = os.environ.get(name, FILE_ENVIRONMENT.get(name, "")).strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def optional(name: str, fallback: str) -> str:
    """Return a non-empty explicit owner-job setting or a documented fallback."""
    return os.environ.get(name, FILE_ENVIRONMENT.get(name, "")).strip() or fallback


def main() -> None:
    owner_user = required("DB_OWNER_USER")
    owner_password = required("DB_OWNER_PASSWORD")
    app_user = required("DB_USER")
    app_password = required("DB_PASSWORD")
    if not ROLE_NAME.fullmatch(owner_user) or not ROLE_NAME.fullmatch(app_user):
        raise RuntimeError("DB_OWNER_USER and DB_USER must be lowercase PostgreSQL role names")
    if owner_user == app_user:
        raise RuntimeError("DB_USER must be a distinct non-owner application role")

    connection = psycopg2.connect(
        # The web process commonly uses the Compose service name; a host-side
        # owner job must instead use DB_OWNER_HOST=127.0.0.1 (or its real
        # managed-Postgres endpoint).  Keep that override separate so web
        # connectivity never silently changes during a migration.
        host=optional("DB_OWNER_HOST", required("DB_HOST")),
        port=optional("DB_OWNER_PORT", required("DB_PORT")),
        dbname=required("DB_NAME"),
        user=owner_user,
        password=owner_password,
    )
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT current_user")
            if cur.fetchone()[0] != owner_user:
                raise RuntimeError("DB_OWNER_USER did not authenticate as the expected schema owner")
            _apply_pending_migrations(cur)
            statement = "CREATE ROLE" if _role_missing(cur, app_user) else "ALTER ROLE"
            cur.execute(
                sql.SQL(
                    "{} {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(
                    sql.SQL(statement),
                    sql.Identifier(app_user),
                    sql.Literal(app_password),
                )
            )
            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(required("DB_NAME")), sql.Identifier(app_user)
            ))
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(app_user)))
            cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            cur.execute(sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public TO {}"
            ).format(sql.Identifier(app_user)))
            cur.execute(sql.SQL(
                "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}"
            ).format(sql.Identifier(app_user)))
            cur.execute(sql.SQL("REVOKE ALL PRIVILEGES ON TABLE public.t_alarms FROM {}").format(sql.Identifier(app_user)))
            cur.execute(sql.SQL("GRANT SELECT ON TABLE public.t_alarms TO {}").format(sql.Identifier(app_user)))
            cur.execute("SELECT has_table_privilege(%s, 'public.t_alarms', 'INSERT')", (app_user,))
            if cur.fetchone()[0]:
                raise RuntimeError("application role still has INSERT on t_alarms")
            cur.execute(
                """
                SELECT c.relowner = (SELECT oid FROM pg_roles WHERE rolname = %s)
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = 't_alarms'
                """,
                (app_user,),
            )
            if cur.fetchone()[0]:
                raise RuntimeError("application role must not own t_alarms")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _role_missing(cursor, role_name: str) -> bool:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role_name,))
    return cursor.fetchone() is None


def _apply_pending_migrations(cursor) -> None:
    """Apply release migrations as owner before the web role loses DDL access."""
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    cursor.execute("SELECT version FROM schema_migrations")
    applied = {row[0] for row in cursor.fetchall()}
    for path in sorted((REPO_ROOT / "init-db").glob("migration_*.sql")):
        matched = MIGRATION_NAME.match(path.name)
        if matched is None or matched.group(1) in applied:
            continue
        cursor.execute(path.read_text(encoding="utf-8"))
        cursor.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s)",
            (matched.group(1),),
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"database role provisioning failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
