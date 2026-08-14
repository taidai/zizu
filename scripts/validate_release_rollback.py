"""Fail closed before an operator rolls a ZiZu host back to a prior release lock.

This script deliberately does not start containers.  It establishes that the
selected immutable lock is compatible with the current database and site
configuration; only then may the operator render the matching digest-only
environment and switch Compose in the approved maintenance procedure.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

import psycopg2

try:  # support direct script and module invocation
    from provision_database_roles import optional, required
    from release_preflight import _latest_migration_version, _read_manifest, verify_release
except ModuleNotFoundError:  # pragma: no cover
    from scripts.provision_database_roles import optional, required
    from scripts.release_preflight import _latest_migration_version, _read_manifest, verify_release


def _schema_version(cursor) -> str:
    cursor.execute("SELECT version FROM schema_migrations ORDER BY version::integer DESC LIMIT 1")
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("database has no applied schema migration")
    return row[0]


def _lock(cursor, lock_id: UUID) -> tuple:
    cursor.execute(
        """
        SELECT platform_version, platform_image, edge_proxy_image, architecture,
               schema_version, site_configuration_version, package_id,
               package_version, package_digest
        FROM t_release_locks
        WHERE id = %s
        """,
        (lock_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("rollback lock was not found")
    return row


def _current_configuration(cursor) -> tuple:
    cursor.execute(
        """
        SELECT state.current_version, configuration.package_digest,
               configuration.secret_references
        FROM t_site_configuration_state state
        JOIN t_site_configuration_versions configuration
          ON configuration.version = state.current_version
        WHERE state.singleton = TRUE
        """
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("current site configuration is unavailable")
    return row


def _safe_secret_references(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(reference, str) and reference.startswith("secret://")
        for reference in value.values()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an immutable ZiZu release rollback")
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--migrations-dir", type=Path, required=True)
    parser.add_argument("--architecture", choices=("linux/amd64", "linux/arm64"), required=True)
    parser.add_argument("--lock-id", type=UUID, required=True)
    arguments = parser.parse_args(argv)

    document = _read_manifest(arguments.release)
    summary = verify_release(document, _latest_migration_version(arguments.migrations_dir))
    owner_user = required("DB_OWNER_USER")
    connection = psycopg2.connect(
        host=optional("DB_OWNER_HOST", required("DB_HOST")),
        port=optional("DB_OWNER_PORT", required("DB_PORT")),
        dbname=required("DB_NAME"),
        user=owner_user,
        password=required("DB_OWNER_PASSWORD"),
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            if cursor.fetchone()[0] != owner_user:
                raise RuntimeError("DB_OWNER_USER did not authenticate as the expected schema owner")
            lock = _lock(cursor, arguments.lock_id)
            current_schema = _schema_version(cursor)
            current_version, current_digest, secret_references = _current_configuration(cursor)
    finally:
        connection.close()

    (
        platform_version,
        platform_image,
        edge_proxy_image,
        architecture,
        lock_schema,
        site_version,
        _package_id,
        _package_version,
        package_digest,
    ) = lock
    if architecture != arguments.architecture:
        raise RuntimeError("rollback lock architecture does not match the target")
    if current_schema != lock_schema or summary["schema_version"] != lock_schema:
        raise RuntimeError("rollback across schema versions is forbidden without an approved reversible migration")
    if (
        summary["platform_version"] != platform_version
        or document["images"][architecture] != platform_image
        or document["edge_proxy_image"] != edge_proxy_image
    ):
        raise RuntimeError("release manifest does not exactly match the verified rollback lock")
    if current_version != site_version or (site_version > 0 and current_digest != package_digest):
        raise RuntimeError("current site configuration is not compatible with the rollback lock")
    if not _safe_secret_references(secret_references):
        raise RuntimeError("current site configuration contains an invalid secret reference")

    print(
        json.dumps(
            {
                "status": "rollback_compatible",
                "lock_id": str(arguments.lock_id),
                "platform_version": platform_version,
                "schema_version": lock_schema,
                "architecture": architecture,
                "site_configuration_version": site_version,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"release rollback validation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
