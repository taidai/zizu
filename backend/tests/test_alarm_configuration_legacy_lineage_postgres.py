from __future__ import annotations

import os
from pathlib import Path
import unittest
from uuid import UUID
from uuid import uuid4

import httpx
import psycopg2
from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.security import get_identity
from app.services.identity import (
    Identity,
    InMemoryIdentityRepository,
    UserIdentity,
    hash_password,
)

from tests.test_alarm_configuration_postgres import (
    MIGRATION_034,
    MIGRATION_035,
    MIGRATION_036,
    _PostgresAlarmConfigurationTestBase,
)


MIGRATION_037 = (
    Path(__file__).resolve().parents[2]
    / "init-db"
    / "migration_037_alarm_configuration_application_kinds.sql"
)


@unittest.skipUnless(
    os.environ.get("ZIZU_POSTGRES_TEST") == "1",
    "set ZIZU_POSTGRES_TEST=1 to run the isolated Postgres alarm configuration seam",
)
class LegacyAlarmConfigurationLineagePostgresTest(
    _PostgresAlarmConfigurationTestBase,
    unittest.IsolatedAsyncioTestCase,
):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.password = "correct horse battery staple"
        cls.password_hash = hash_password(
            cls.password,
            salt=b"legacy-lineage!",
        )

    def setUp(self) -> None:
        self._reset_schema_through_032()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                self.installation_id, _ = self._insert_installed_site(cursor)
                self.entity_id, _ = self._insert_entities(cursor, self.installation_id)
                cursor.execute(
                    """
                    UPDATE t_tags
                    SET alarm_level = 'error1'
                    WHERE id = (
                        SELECT tag_id FROM t_entity_instance_bindings
                        ORDER BY tag_id LIMIT 1
                    )
                    RETURNING id
                    """
                )
                self.legacy_tag_id = cursor.fetchone()[0]
                for migration in (
                    MIGRATION_034,
                    MIGRATION_035,
                    MIGRATION_036,
                    MIGRATION_037,
                ):
                    cursor.execute(migration.read_text(encoding="utf-8"))

    def _repository(self):
        from app.services.alarm_configuration_postgres import (
            PostgresAlarmConfigurationRepository,
        )

        return PostgresAlarmConfigurationRepository(
            connection_factory=lambda: psycopg2.connect(**self.connection_kwargs)
        )

    def _app(self) -> FastAPI:
        from app.api.alarm_configurations import get_alarm_configuration, router
        from app.services.alarm_configuration import AlarmConfiguration

        identities = InMemoryIdentityRepository([
            UserIdentity(
                UUID("00000000-0000-0000-0000-000000000002"),
                "engineer",
                self.password_hash,
                "engineer",
                "active",
            ),
        ])
        app = FastAPI()
        app.include_router(auth_router, prefix="/api/v1")
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: Identity(identities)
        app.dependency_overrides[get_alarm_configuration] = lambda: AlarmConfiguration(
            self._repository()
        )
        return app

    async def _login(self, client: httpx.AsyncClient) -> dict[str, str]:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": "engineer", "password": self.password},
        )
        self.assertEqual(200, response.status_code, response.text)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def _runtime_counts(self) -> dict[str, int]:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_version FROM t_site_configuration_state"
                )
                counts = {"site_version": int(cursor.fetchone()[0])}
                for table in (
                    "t_solution_install_plans",
                    "t_solution_installations",
                    "t_site_configuration_versions",
                    "t_alarm_definitions",
                    "t_alarm_definition_current",
                    "t_alarm_definition_origins",
                    "t_legacy_alarm_migrations",
                    "t_legacy_alarm_migration_targets",
                    "t_solution_delivery_audit",
                    "t_alarm_configuration_idempotency",
                ):
                    cursor.execute(f"SELECT count(*) FROM {table}")
                    counts[table] = int(cursor.fetchone()[0])
                return counts

    async def test_public_legacy_plan_is_zero_runtime_write_and_generic_apply_creates_lineage(self) -> None:
        from app.services.alarm_configuration_postgres import (
            load_latest_applied_alarm_configuration,
        )

        before_plan = self._runtime_counts()
        transport = httpx.ASGITransport(app=self._app())
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://testserver",
        ) as client:
            headers = await self._login(client)
            planned = await client.post(
                "/api/v1/alarm-configuration-migrations/legacy/plans",
                headers=headers,
                json={"installation_id": self.installation_id, "selections": []},
            )

            self.assertEqual(201, planned.status_code, planned.text)
            plan = planned.json()
            self.assertEqual("legacy_migration", plan["kind"])
            self.assertIsNone(plan["rule_set_revision"])
            self.assertEqual("ready", plan["status"])
            self.assertEqual(["add"], [item["action"] for item in plan["items"]])
            self.assertEqual([], plan["blockers"])

            after_plan = self._runtime_counts()
            self.assertEqual(before_plan, after_plan)

            applied_response = await client.post(
                f"/api/v1/alarm-configuration-plans/{plan['id']}/apply",
                headers={**headers, "Idempotency-Key": "legacy-public-apply"},
                json={"plan_digest": plan["digest"]},
            )
            replay_response = await client.post(
                f"/api/v1/alarm-configuration-plans/{plan['id']}/apply",
                headers={**headers, "Idempotency-Key": "legacy-public-apply"},
                json={"plan_digest": plan["digest"]},
            )

        self.assertEqual(200, applied_response.status_code, applied_response.text)
        self.assertEqual(applied_response.json(), replay_response.json())
        application = applied_response.json()
        self.assertEqual(2, application["site_configuration_version"])
        self.assertEqual(1, len(application["definition_ids"]))

        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_version FROM t_site_configuration_state"
                )
                self.assertEqual(2, cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT definition.installation_id,
                           definition.site_configuration_version,
                           current.definition_id
                    FROM t_alarm_definitions definition
                    JOIN t_alarm_definition_current current
                      ON current.definition_id = definition.id
                    WHERE definition.id = %s
                    """,
                    (application["definition_ids"][0],),
                )
                derived_installation_id, site_version, current_id = cursor.fetchone()
                self.assertNotEqual(UUID(self.installation_id), derived_installation_id)
                self.assertEqual(2, site_version)
                self.assertEqual(UUID(application["definition_ids"][0]), current_id)

                cursor.execute(
                    """
                    SELECT plan_kind, rule_set_id, rule_set_revision,
                           application_id
                    FROM t_alarm_configuration_plans
                    WHERE status = 'applied'
                    """
                )
                plan_kind, rule_set_id, revision, application_id = cursor.fetchone()
                self.assertEqual("legacy_migration", plan_kind)
                self.assertIsNone(rule_set_id)
                self.assertIsNone(revision)
                self.assertIsNotNone(application_id)

                applied = load_latest_applied_alarm_configuration(connection)
                self.assertIsNotNone(applied)
                self.assertEqual(application_id, applied.id)
                self.assertEqual(derived_installation_id, applied.installation_id)
                self.assertEqual(2, applied.site_configuration_version)
                self.assertEqual(
                    (UUID(application["definition_ids"][0]),),
                    applied.definition_ids,
                )
                self.assertEqual(("add",), tuple(item.action for item in applied.items))

                cursor.execute(
                    """
                    SELECT count(*) FROM t_solution_delivery_audit
                    WHERE action = 'solution.install'
                      AND installation_id = %s
                    """,
                    (derived_installation_id,),
                )
                self.assertEqual(1, cursor.fetchone()[0])
                cursor.execute(
                    "SELECT plan_id FROM t_legacy_alarm_migrations"
                )
                self.assertEqual(plan["id"], str(cursor.fetchone()[0]))

    async def test_generic_apply_rejects_stale_legacy_source_without_partial_rows(self) -> None:
        transport = httpx.ASGITransport(app=self._app())
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self._login(client)
            planned = await client.post(
                "/api/v1/alarm-configuration-migrations/legacy/plans",
                headers=headers,
                json={"installation_id": self.installation_id, "selections": []},
            )
            self.assertEqual(201, planned.status_code, planned.text)
            plan = planned.json()
            before_apply = self._runtime_counts()
            with psycopg2.connect(**self.connection_kwargs) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE t_entity_instance_bindings
                        SET active = FALSE
                        WHERE tag_id = %s
                        """,
                        (self.legacy_tag_id,),
                    )
            response = await client.post(
                f"/api/v1/alarm-configuration-plans/{plan['id']}/apply",
                headers={**headers, "Idempotency-Key": "stale-legacy-source"},
                json={"plan_digest": plan["digest"]},
            )
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("ALARM_MIGRATION_PLAN_STALE", response.json()["detail"]["code"])
        self.assertEqual(before_apply, self._runtime_counts())

    async def test_invalid_explicit_selection_writes_no_plan_or_runtime_rows(self) -> None:
        before = self._runtime_counts()
        transport = httpx.ASGITransport(app=self._app())
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            headers = await self._login(client)
            response = await client.post(
                "/api/v1/alarm-configuration-migrations/legacy/plans",
                headers=headers,
                json={
                    "installation_id": self.installation_id,
                    "selections": [{
                        "source_kind": "tag_alarm",
                        "source_key": str(self.legacy_tag_id),
                        "entity_instance_id": str(uuid4()),
                    }],
                },
            )
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("ALARM_MIGRATION_SELECTION_INVALID", response.json()["detail"]["code"])
        self.assertEqual(before, self._runtime_counts())
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM t_alarm_configuration_plans")
                self.assertEqual(0, cursor.fetchone()[0])

    def test_migration_037_replays_and_has_conditional_lineage_constraints(self) -> None:
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(MIGRATION_037.read_text(encoding="utf-8"))
                cursor.execute(MIGRATION_037.read_text(encoding="utf-8"))
                cursor.execute(
                    """
                    SELECT is_nullable FROM information_schema.columns
                    WHERE table_name = 't_alarm_configuration_plans'
                      AND column_name = 'rule_set_id'
                    """
                )
                self.assertEqual("YES", cursor.fetchone()[0])
                cursor.execute(
                    """
                    SELECT count(*) FROM pg_constraint
                    WHERE conname IN (
                        'chk_alarm_configuration_plan_kind',
                        'chk_legacy_alarm_migration_plan'
                    )
                    """
                )
                self.assertEqual(2, cursor.fetchone()[0])

    def test_migration_037_fails_closed_for_unversioned_legacy_evidence(self) -> None:
        from app.services.alarm_definitions import (
            AlarmDefinitionPlan,
            InstalledAlarmDefinition,
        )
        from app.services.alarm_postgres import PostgresAlarmDefinitionCatalog

        self._reset_schema_through_032()
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                installation_id, _ = self._insert_installed_site(cursor)
                entity_id, _ = self._insert_entities(cursor, installation_id)
                for migration in (MIGRATION_034, MIGRATION_035, MIGRATION_036):
                    cursor.execute(migration.read_text(encoding="utf-8"))
        definition = InstalledAlarmDefinition(
            id=uuid4(),
            asset_id="site.alarm.legacy.unversioned",
            version="legacy-migration:1",
            installation_id=UUID(installation_id),
            site_configuration_version=1,
            entity_instance_id=UUID(entity_id),
            entity_definition_id="pcs.activePower",
            trigger={"operator": "gt", "value": 90},
            trigger_duration_seconds=0,
            recovery={"operator": "lt", "value": 80},
            recovery_duration_seconds=0,
            severity="WARNING",
            notification_throttle_seconds=0,
        )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            PostgresAlarmDefinitionCatalog().install_definitions(
                AlarmDefinitionPlan(
                    installation_id=definition.installation_id,
                    site_configuration_version=1,
                    package_digest="a" * 64,
                    definitions=(definition,),
                    digest="f" * 64,
                ),
                transaction=connection,
            )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO t_alarm_definition_origins
                      (definition_id, origin_type, source_kind, source_key,
                       details, actor)
                    VALUES (%s, 'legacy_migration', 'tag_alarm',
                            'unversioned', '{}', 'user:legacy')
                    """,
                    (definition.id,),
                )
                migration_id = uuid4()
                cursor.execute(
                    """
                    INSERT INTO t_legacy_alarm_migrations
                      (id, source_kind, source_key, state, actor, details)
                    VALUES (%s, 'tag_alarm', 'unversioned', 'migrated',
                            'user:legacy', '{}')
                    """,
                    (migration_id,),
                )
                cursor.execute(
                    """
                    INSERT INTO t_legacy_alarm_migration_targets
                      (migration_id, definition_id, source_kind, source_key,
                       origin_type)
                    VALUES (%s, %s, 'tag_alarm', 'unversioned',
                            'legacy_migration')
                    """,
                    (migration_id, definition.id),
                )
        with psycopg2.connect(**self.connection_kwargs) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                with self.assertRaisesRegex(
                    psycopg2.Error,
                    "no versioned application lineage",
                ):
                    cursor.execute(MIGRATION_037.read_text(encoding="utf-8"))

    def test_legacy_evidence_rejects_a_rule_set_plan_id(self) -> None:
        from app.services.alarm_configuration import (
            AlarmConfiguration,
            AlarmRule,
            EntitySelection,
            PlanAlarmConfiguration,
        )

        service = AlarmConfiguration(self._repository())
        revision = service.create_rule_set(
            key="not-a-legacy-plan",
            name="Not a legacy plan",
            rules=(AlarmRule(
                id="warning",
                name="Warning",
                severity="WARNING",
                trigger={"operator": "gte", "value": 90},
                trigger_duration_seconds=0,
                recovery={"operator": "lt", "value": 80},
                recovery_duration_seconds=0,
                notification_throttle_seconds=0,
                unit="kW",
            ),),
            actor="user:engineer",
        )
        rule_set_plan = service.plan(PlanAlarmConfiguration(
            installation_id=UUID(self.installation_id),
            selection=EntitySelection(entity_instance_ids=(UUID(self.entity_id),)),
            rule_set_id=revision.rule_set_id,
            rule_set_revision=revision.revision,
            planned_by="user:engineer",
        ))
        statements = (
            (
                """
                INSERT INTO t_alarm_definition_origins
                  (definition_id, origin_type, source_kind, source_key,
                   plan_id, details, actor)
                VALUES (%s, 'legacy_migration', 'tag_alarm', 'wrong-plan',
                        %s, '{}', 'user:engineer')
                """,
                (uuid4(), rule_set_plan.id),
            ),
            (
                """
                INSERT INTO t_legacy_alarm_migrations
                  (id, source_kind, source_key, state, actor, details, plan_id)
                VALUES (%s, 'tag_alarm', 'wrong-plan', 'migrated',
                        'user:engineer', %s, %s)
                """,
                (
                    uuid4(),
                    '{"plan_id": "' + str(rule_set_plan.id) + '"}',
                    rule_set_plan.id,
                ),
            ),
        )
        for statement, parameters in statements:
            with self.subTest(statement=statement):
                with psycopg2.connect(**self.connection_kwargs) as connection:
                    with connection.cursor() as cursor:
                        with self.assertRaisesRegex(
                            psycopg2.errors.RaiseException,
                            "legacy alarm evidence requires a legacy_migration plan",
                        ):
                            cursor.execute(statement, parameters)


if __name__ == "__main__":
    unittest.main()
