"""Record deployment evidence after a public ZiZu release becomes reachable.

This is an owner-only release job.  The web application receives SELECT-only
access to release locks, so it cannot forge the evidence shown by health or a
delivery report.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse
from pathlib import Path
from uuid import uuid4

import psycopg2

try:  # direct ``python scripts/...`` and ``python -m scripts...`` both work
    from provision_database_roles import optional, required
    from release_preflight import _latest_migration_version, _read_manifest, verify_release
except ModuleNotFoundError:  # pragma: no cover - exercised by the module entrypoint
    from scripts.provision_database_roles import optional, required
    from scripts.release_preflight import _latest_migration_version, _read_manifest, verify_release


def _public_liveness(url: str, platform_version: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("public liveness endpoint must use HTTPS")
    endpoint = f"{url.rstrip('/')}/api/v1/health/live"
    try:
        with urllib.request.urlopen(endpoint, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as error:
        raise RuntimeError(f"public liveness check failed: {error}") from error
    if body != {"status": "alive", "version": platform_version}:
        raise RuntimeError("public liveness does not match the release platform version")


def _docker_inspect(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        message = result.stderr.strip() or "no inspect value returned"
        raise RuntimeError(f"runtime image verification failed: {message}")
    return value


def _verified_runtime_image(container: str, expected_image: str, architecture: str) -> str:
    """Bind the lock to the container actually running on this host."""
    expected_id = _docker_inspect(
        ["docker", "image", "inspect", "--format", "{{.Id}}", expected_image]
    )
    actual_id = _docker_inspect(
        ["docker", "inspect", "--format", "{{.Image}}", container]
    )
    expected_architecture = _docker_inspect(
        ["docker", "image", "inspect", "--format", "{{.Architecture}}", expected_image]
    )
    if actual_id != expected_id:
        raise RuntimeError(f"container {container!r} is not running the declared release image")
    if expected_architecture != architecture.removeprefix("linux/"):
        raise RuntimeError("declared release architecture does not match the loaded platform image")
    return expected_id


def _current_site_configuration(cursor) -> tuple[int, str | None, str | None, str | None]:
    cursor.execute(
        """
        SELECT state.current_version, package.package_id, package.version,
               configuration.package_digest
        FROM t_site_configuration_state state
        JOIN t_site_configuration_versions configuration
          ON configuration.version = state.current_version
        LEFT JOIN t_solution_packages package
          ON package.id = configuration.package_record_id
        WHERE state.singleton = TRUE
        """
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("current site configuration is unavailable")
    return row


def _schema_version(cursor) -> str:
    cursor.execute(
        "SELECT version FROM schema_migrations ORDER BY version::integer DESC LIMIT 1"
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("database has no applied schema migration")
    return row[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record a verified ZiZu immutable deployment lock")
    parser.add_argument("--release", type=Path, required=True, help="verified release.json")
    parser.add_argument("--migrations-dir", type=Path, required=True, help="migration directory shipped by the release")
    parser.add_argument("--architecture", choices=("linux/amd64", "linux/arm64"), required=True)
    parser.add_argument("--public-api", required=True, help="public HTTPS base URL used by engineers")
    parser.add_argument("--backend-container", required=True, help="running backend container ID or name")
    parser.add_argument("--edge-container", required=True, help="running TLS edge-proxy container ID or name")
    arguments = parser.parse_args(argv)

    document = _read_manifest(arguments.release)
    expected_schema = _latest_migration_version(arguments.migrations_dir)
    summary = verify_release(document, expected_schema)
    platform_image = document["images"][arguments.architecture]
    platform_image_id = _verified_runtime_image(
        arguments.backend_container, platform_image, arguments.architecture
    )
    edge_proxy_image_id = _verified_runtime_image(
        arguments.edge_container, document["edge_proxy_image"], arguments.architecture
    )
    _public_liveness(arguments.public_api, summary["platform_version"])

    owner_user = required("DB_OWNER_USER")
    lock_id = uuid4()
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
            if _schema_version(cursor) != summary["schema_version"]:
                raise RuntimeError("database schema does not match the verified release")
            site_version, package_id, package_version, package_digest = _current_site_configuration(cursor)
            cursor.execute(
                """
                INSERT INTO t_release_locks
                    (id, platform_version, platform_image, platform_image_id,
                     edge_proxy_image, edge_proxy_image_id, architecture,
                     schema_version, site_configuration_version,
                     package_id, package_version, package_digest)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    lock_id,
                    summary["platform_version"],
                    platform_image,
                    platform_image_id,
                    document["edge_proxy_image"],
                    edge_proxy_image_id,
                    arguments.architecture,
                    summary["schema_version"],
                    site_version,
                    package_id,
                    package_version,
                    package_digest,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(
        json.dumps(
            {
                "status": "recorded",
                "lock_id": str(lock_id),
                **summary,
                "architecture": arguments.architecture,
                "site_configuration_version": site_version,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"release lock recording failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
