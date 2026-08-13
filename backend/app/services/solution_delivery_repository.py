"""解决方案交付的内存与 Postgres 持久化 Adapter。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

from psycopg2.extras import Json

from app.services.solution_delivery_contracts import (
    DeliveryError,
    DeliveryReport,
    InstallationOutcome,
    InstallationPlan,
    PackageImport,
)

__all__ = [
    "InMemoryDeliveryRepository",
    "PostgresDeliveryRepository",
]


class InMemoryDeliveryRepository:
    """辅助测试使用的持久化 Adapter。"""

    def __init__(self) -> None:
        self._packages: dict[tuple[str, str], PackageImport] = {}
        self._packages_by_id: dict[UUID, PackageImport] = {}
        self._plans: dict[UUID, InstallationPlan] = {}
        self._installations: dict[UUID, InstallationOutcome] = {}
        self._idempotency: dict[
            tuple[str, str, str], tuple[str, InstallationOutcome | DeliveryReport]
        ] = {}
        self._reports: dict[UUID, DeliveryReport] = {}
        self._site_configuration_version = 0

    def save_package(self, package: PackageImport) -> PackageImport:
        key = (package.package_id, package.version)
        existing = self._packages.get(key)
        if existing is not None:
            if existing.digest != package.digest:
                raise DeliveryError(
                    "PACKAGE_DIGEST_CONFLICT",
                    "Package id and version already exist with different content",
                )
            return existing
        self._packages[key] = package
        self._packages_by_id[package.id] = package
        return package

    def list_packages(self) -> list[PackageImport]:
        return sorted(
            self._packages.values(),
            key=lambda package: (package.package_id, package.version),
        )

    def get_package(self, package_record_id: UUID) -> PackageImport | None:
        return self._packages_by_id.get(package_record_id)

    def site_configuration_version(self) -> int:
        return self._site_configuration_version

    def save_plan(self, plan: InstallationPlan) -> InstallationPlan:
        existing = next(
            (
                candidate
                for candidate in self._plans.values()
                if candidate.digest == plan.digest
            ),
            None,
        )
        if existing is not None:
            return existing
        self._plans[plan.id] = plan
        return plan

    def get_plan(self, plan_id: UUID) -> InstallationPlan | None:
        return self._plans.get(plan_id)

    def get_idempotent_installation(
        self,
        actor: str,
        key: str,
        request_digest: str,
    ) -> InstallationOutcome | None:
        existing = self._idempotency.get(("apply_install", actor, key))
        if existing is None:
            return None
        existing_digest, outcome = existing
        if existing_digest != request_digest:
            raise DeliveryError(
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency key was already used for another request",
            )
        return outcome

    def install(
        self,
        plan: InstallationPlan,
        actor: str,
        key: str,
        request_digest: str,
    ) -> InstallationOutcome:
        existing_installation = self.find_installation(
            plan.package_record_id,
            plan.package_digest,
        )
        if existing_installation is not None:
            self._idempotency[("apply_install", actor, key)] = (
                request_digest,
                existing_installation,
            )
            return existing_installation
        if self._site_configuration_version != plan.base_site_configuration_version:
            raise DeliveryError(
                "INSTALL_PLAN_STALE",
                "Site configuration changed after the plan was created",
            )
        self._site_configuration_version += 1
        outcome = InstallationOutcome(
            id=uuid4(),
            plan_id=plan.id,
            package_record_id=plan.package_record_id,
            package_digest=plan.package_digest,
            site_configuration_version=self._site_configuration_version,
            status="installed",
        )
        self._installations[outcome.id] = outcome
        self._idempotency[("apply_install", actor, key)] = (
            request_digest,
            outcome,
        )
        return outcome

    def list_installations(self) -> list[InstallationOutcome]:
        return list(self._installations.values())

    def get_installation(self, installation_id: UUID) -> InstallationOutcome | None:
        return self._installations.get(installation_id)

    def find_installation(
        self,
        package_record_id: UUID,
        package_digest: str,
    ) -> InstallationOutcome | None:
        return next(
            (
                installation
                for installation in self._installations.values()
                if installation.package_record_id == package_record_id
                and installation.package_digest == package_digest
            ),
            None,
        )

    def package_for_installation(
        self,
        installation: InstallationOutcome,
    ) -> PackageImport | None:
        return self._packages_by_id.get(installation.package_record_id)

    def get_idempotent_report(
        self,
        actor: str,
        key: str,
        request_digest: str,
    ) -> DeliveryReport | None:
        existing = self._idempotency.get(("run_acceptance", actor, key))
        if existing is None:
            return None
        existing_digest, report = existing
        if existing_digest != request_digest:
            raise DeliveryError(
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency key was already used for another request",
            )
        if not isinstance(report, DeliveryReport):
            raise RuntimeError("Acceptance idempotency record points to another result type")
        return report

    def save_report(
        self,
        report: DeliveryReport,
        actor: str,
        key: str,
        request_digest: str,
    ) -> DeliveryReport:
        self._reports[report.id] = report
        self._idempotency[("run_acceptance", actor, key)] = (
            request_digest,
            report,
        )
        return report

    def get_report(self, report_id: UUID) -> DeliveryReport | None:
        return self._reports.get(report_id)


class PostgresDeliveryRepository:
    """生产 Postgres 持久化 Adapter。"""

    @staticmethod
    @contextmanager
    def _connection():
        from app.services.telemetry_store import get_connection

        with get_connection() as connection:
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @staticmethod
    def _package_from_row(row: tuple[Any, ...], assets: dict[str, bytes]) -> PackageImport:
        return PackageImport(
            id=row[0],
            package_id=row[1],
            version=row[2],
            display_name=row[3],
            digest=row[4],
            status=row[5],
            acceptance_ids=tuple(row[6]),
            manifest=row[7],
            assets=assets,
        )

    @staticmethod
    def _plan_from_row(row: tuple[Any, ...]) -> InstallationPlan:
        return InstallationPlan(
            id=row[0],
            package_record_id=row[1],
            package_digest=row[2],
            base_site_configuration_version=row[3],
            status=row[4],
            items=tuple(row[5]),
            blockers=tuple(row[6]),
            digest=row[7],
        )

    @staticmethod
    def _installation_from_row(row: tuple[Any, ...]) -> InstallationOutcome:
        return InstallationOutcome(
            id=row[0],
            plan_id=row[1],
            package_record_id=row[2],
            package_digest=row[3],
            site_configuration_version=row[4],
            status=row[5],
        )

    @staticmethod
    def _report_from_row(row: tuple[Any, ...]) -> DeliveryReport:
        return DeliveryReport(
            id=row[0],
            installation_id=row[1],
            platform_version=row[2],
            package_id=row[3],
            package_version=row[4],
            package_digest=row[5],
            site_configuration_version=row[6],
            actor=row[7],
            started_at=row[8].isoformat(),
            finished_at=row[9].isoformat(),
            duration_ms=row[10],
            status=row[11],
            items=tuple(row[12]),
            digest=row[13],
        )

    @staticmethod
    def _load_assets(cur: Any, package_record_id: UUID) -> dict[str, bytes]:
        cur.execute(
            "SELECT path, content FROM t_solution_package_assets "
            "WHERE package_record_id = %s ORDER BY path",
            (package_record_id,),
        )
        return {path: bytes(content) for path, content in cur.fetchall()}

    def save_package(self, package: PackageImport) -> PackageImport:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO t_solution_packages
                      (id, package_id, version, display_name, digest, status,
                       acceptance_ids, manifest)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (package_id, version) DO NOTHING
                    RETURNING id
                    """,
                    (
                        package.id,
                        package.package_id,
                        package.version,
                        package.display_name,
                        package.digest,
                        package.status,
                        Json(list(package.acceptance_ids)),
                        Json(package.manifest),
                    ),
                )
                inserted = cur.fetchone()
                if inserted:
                    for path, content in package.assets.items():
                        cur.execute(
                            "INSERT INTO t_solution_package_assets "
                            "(package_record_id, path, content) VALUES (%s, %s, %s)",
                            (package.id, path, content),
                        )
                    conn.commit()
                    return package

                cur.execute(
                    """
                    SELECT id, package_id, version, display_name, digest, status,
                           acceptance_ids, manifest
                    FROM t_solution_packages
                    WHERE package_id = %s AND version = %s
                    """,
                    (package.package_id, package.version),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError("Package conflict row disappeared")
                if row[4] != package.digest:
                    raise DeliveryError(
                        "PACKAGE_DIGEST_CONFLICT",
                        "Package id and version already exist with different content",
                    )
                assets = self._load_assets(cur, row[0])
                conn.commit()
                return self._package_from_row(row, assets)

    def list_packages(self) -> list[PackageImport]:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, package_id, version, display_name, digest, status,
                           acceptance_ids, manifest
                    FROM t_solution_packages
                    ORDER BY package_id, version
                    """
                )
                rows = cur.fetchall()
                packages = [
                    self._package_from_row(row, self._load_assets(cur, row[0]))
                    for row in rows
                ]
                conn.commit()
                return packages

    def get_package(self, package_record_id: UUID) -> PackageImport | None:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, package_id, version, display_name, digest, status,
                           acceptance_ids, manifest
                    FROM t_solution_packages WHERE id = %s
                    """,
                    (package_record_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                package = self._package_from_row(
                    row,
                    self._load_assets(cur, package_record_id),
                )
                conn.commit()
                return package

    def site_configuration_version(self) -> int:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_version FROM t_site_configuration_state "
                    "WHERE singleton = TRUE"
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0

    def save_plan(self, plan: InstallationPlan) -> InstallationPlan:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO t_solution_install_plans
                      (id, package_record_id, package_digest,
                       base_site_configuration_version, status, items, blockers, digest)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (digest) DO NOTHING
                    RETURNING id, package_record_id, package_digest,
                              base_site_configuration_version, status,
                              items, blockers, digest
                    """,
                    (
                        plan.id,
                        plan.package_record_id,
                        plan.package_digest,
                        plan.base_site_configuration_version,
                        plan.status,
                        Json(list(plan.items)),
                        Json(list(plan.blockers)),
                        plan.digest,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        SELECT id, package_record_id, package_digest,
                               base_site_configuration_version, status,
                               items, blockers, digest
                        FROM t_solution_install_plans WHERE digest = %s
                        """,
                        (plan.digest,),
                    )
                    row = cur.fetchone()
                if row is None:
                    raise RuntimeError("Installation plan conflict row disappeared")
                return self._plan_from_row(row)

    def get_plan(self, plan_id: UUID) -> InstallationPlan | None:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, package_record_id, package_digest,
                           base_site_configuration_version, status, items, blockers, digest
                    FROM t_solution_install_plans WHERE id = %s
                    """,
                    (plan_id,),
                )
                row = cur.fetchone()
                return self._plan_from_row(row) if row else None

    def _idempotency_row(
        self,
        cur: Any,
        command_type: str,
        actor: str,
        key: str,
    ) -> tuple[Any, ...] | None:
        cur.execute(
            """
            SELECT request_digest, installation_id, report_id
            FROM t_delivery_idempotency
            WHERE command_type = %s AND actor = %s AND idempotency_key = %s
            """,
            (command_type, actor, key),
        )
        return cur.fetchone()

    def get_idempotent_installation(
        self,
        actor: str,
        key: str,
        request_digest: str,
    ) -> InstallationOutcome | None:
        with self._connection() as conn:
            with conn.cursor() as cur:
                existing = self._idempotency_row(cur, "apply_install", actor, key)
                if existing is None:
                    return None
                if existing[0] != request_digest:
                    raise DeliveryError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency key was already used for another request",
                    )
                cur.execute(
                    """
                    SELECT id, plan_id, package_record_id, package_digest,
                           site_configuration_version, status
                    FROM t_solution_installations WHERE id = %s
                    """,
                    (existing[1],),
                )
                row = cur.fetchone()
                return self._installation_from_row(row) if row else None

    def install(
        self,
        plan: InstallationPlan,
        actor: str,
        key: str,
        request_digest: str,
    ) -> InstallationOutcome:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_version FROM t_site_configuration_state "
                    "WHERE singleton = TRUE FOR UPDATE"
                )
                current_version = int(cur.fetchone()[0])
                existing = self._idempotency_row(cur, "apply_install", actor, key)
                if existing is not None:
                    if existing[0] != request_digest:
                        raise DeliveryError(
                            "IDEMPOTENCY_KEY_REUSED",
                            "Idempotency key was already used for another request",
                        )
                    cur.execute(
                        """
                        SELECT id, plan_id, package_record_id, package_digest,
                               site_configuration_version, status
                        FROM t_solution_installations WHERE id = %s
                        """,
                        (existing[1],),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise RuntimeError("Installation idempotency result is missing")
                    return self._installation_from_row(row)

                cur.execute(
                    """
                    SELECT id, plan_id, package_record_id, package_digest,
                           site_configuration_version, status
                    FROM t_solution_installations
                    WHERE package_record_id = %s AND package_digest = %s
                    ORDER BY site_configuration_version LIMIT 1
                    """,
                    (plan.package_record_id, plan.package_digest),
                )
                existing_installation = cur.fetchone()
                if existing_installation is not None:
                    outcome = self._installation_from_row(existing_installation)
                    cur.execute(
                        """
                        INSERT INTO t_delivery_idempotency
                          (command_type, actor, idempotency_key, request_digest,
                           installation_id)
                        VALUES ('apply_install', %s, %s, %s, %s)
                        """,
                        (actor, key, request_digest, outcome.id),
                    )
                    conn.commit()
                    return outcome
                if current_version != plan.base_site_configuration_version:
                    raise DeliveryError(
                        "INSTALL_PLAN_STALE",
                        "Site configuration changed after the plan was created",
                    )
                outcome = InstallationOutcome(
                    id=uuid4(),
                    plan_id=plan.id,
                    package_record_id=plan.package_record_id,
                    package_digest=plan.package_digest,
                    site_configuration_version=current_version + 1,
                    status="installed",
                )
                cur.execute(
                    """
                    INSERT INTO t_solution_installations
                      (id, plan_id, package_record_id, package_digest,
                       site_configuration_version, status)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        outcome.id,
                        outcome.plan_id,
                        outcome.package_record_id,
                        outcome.package_digest,
                        outcome.site_configuration_version,
                        outcome.status,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO t_site_configuration_versions
                      (version, previous_version, installation_id,
                       package_record_id, package_digest, actor)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        outcome.site_configuration_version,
                        current_version,
                        outcome.id,
                        outcome.package_record_id,
                        outcome.package_digest,
                        actor,
                    ),
                )
                cur.execute(
                    "UPDATE t_site_configuration_state SET current_version = %s "
                    "WHERE singleton = TRUE",
                    (outcome.site_configuration_version,),
                )
                cur.execute(
                    """
                    INSERT INTO t_delivery_idempotency
                      (command_type, actor, idempotency_key, request_digest,
                       installation_id)
                    VALUES ('apply_install', %s, %s, %s, %s)
                    """,
                    (actor, key, request_digest, outcome.id),
                )
                cur.execute(
                    """
                    INSERT INTO t_solution_delivery_audit
                      (id, actor, action, installation_id, package_record_id,
                       package_digest, site_configuration_version, details)
                    VALUES (%s, %s, 'solution.install', %s, %s, %s, %s, %s)
                    """,
                    (
                        uuid4(),
                        actor,
                        outcome.id,
                        outcome.package_record_id,
                        outcome.package_digest,
                        outcome.site_configuration_version,
                        Json({"plan_id": str(plan.id), "plan_digest": plan.digest}),
                    ),
                )
                conn.commit()
                return outcome

    def list_installations(self) -> list[InstallationOutcome]:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, plan_id, package_record_id, package_digest,
                           site_configuration_version, status
                    FROM t_solution_installations ORDER BY site_configuration_version
                    """
                )
                return [self._installation_from_row(row) for row in cur.fetchall()]

    def get_installation(self, installation_id: UUID) -> InstallationOutcome | None:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, plan_id, package_record_id, package_digest,
                           site_configuration_version, status
                    FROM t_solution_installations WHERE id = %s
                    """,
                    (installation_id,),
                )
                row = cur.fetchone()
                return self._installation_from_row(row) if row else None

    def find_installation(
        self,
        package_record_id: UUID,
        package_digest: str,
    ) -> InstallationOutcome | None:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, plan_id, package_record_id, package_digest,
                           site_configuration_version, status
                    FROM t_solution_installations
                    WHERE package_record_id = %s AND package_digest = %s
                    ORDER BY site_configuration_version LIMIT 1
                    """,
                    (package_record_id, package_digest),
                )
                row = cur.fetchone()
                return self._installation_from_row(row) if row else None

    def package_for_installation(
        self,
        installation: InstallationOutcome,
    ) -> PackageImport | None:
        return self.get_package(installation.package_record_id)

    def get_idempotent_report(
        self,
        actor: str,
        key: str,
        request_digest: str,
    ) -> DeliveryReport | None:
        with self._connection() as conn:
            with conn.cursor() as cur:
                existing = self._idempotency_row(cur, "run_acceptance", actor, key)
                if existing is None:
                    return None
                if existing[0] != request_digest:
                    raise DeliveryError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency key was already used for another request",
                    )
                cur.execute(
                    """
                    SELECT id, installation_id, platform_version, package_id,
                           package_version, package_digest, site_configuration_version,
                           actor, started_at, finished_at, duration_ms,
                           status, items, digest
                    FROM t_delivery_reports WHERE id = %s
                    """,
                    (existing[2],),
                )
                row = cur.fetchone()
                return self._report_from_row(row) if row else None

    def save_report(
        self,
        report: DeliveryReport,
        actor: str,
        key: str,
        request_digest: str,
    ) -> DeliveryReport:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_version FROM t_site_configuration_state "
                    "WHERE singleton = TRUE FOR UPDATE"
                )
                existing = self._idempotency_row(cur, "run_acceptance", actor, key)
                if existing is not None:
                    if existing[0] != request_digest:
                        raise DeliveryError(
                            "IDEMPOTENCY_KEY_REUSED",
                            "Idempotency key was already used for another request",
                        )
                    cur.execute(
                        """
                        SELECT id, installation_id, platform_version, package_id,
                               package_version, package_digest,
                               site_configuration_version, actor, started_at,
                               finished_at, duration_ms, status, items, digest
                        FROM t_delivery_reports WHERE id = %s
                        """,
                        (existing[2],),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise RuntimeError("Report idempotency result is missing")
                    return self._report_from_row(row)
                cur.execute(
                    """
                    INSERT INTO t_delivery_reports
                      (id, installation_id, platform_version, package_id,
                       package_version, package_digest, site_configuration_version,
                       actor, started_at, finished_at, duration_ms,
                       status, items, digest)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        report.id,
                        report.installation_id,
                        report.platform_version,
                        report.package_id,
                        report.package_version,
                        report.package_digest,
                        report.site_configuration_version,
                        report.actor,
                        report.started_at,
                        report.finished_at,
                        report.duration_ms,
                        report.status,
                        Json(list(report.items)),
                        report.digest,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO t_delivery_idempotency
                      (command_type, actor, idempotency_key, request_digest,
                       report_id)
                    VALUES ('run_acceptance', %s, %s, %s, %s)
                    """,
                    (actor, key, request_digest, report.id),
                )
                conn.commit()
                return report

    def get_report(self, report_id: UUID) -> DeliveryReport | None:
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, installation_id, platform_version, package_id,
                           package_version, package_digest, site_configuration_version,
                           actor, started_at, finished_at, duration_ms,
                           status, items, digest
                    FROM t_delivery_reports WHERE id = %s
                    """,
                    (report_id,),
                )
                row = cur.fetchone()
                return self._report_from_row(row) if row else None
