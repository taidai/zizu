from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import unittest
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-long-enough")

import httpx
from fastapi import FastAPI

from tests.api_test_client import AuthenticatedApiClient


DEFINITION_ID = UUID("71000000-0000-0000-0000-000000000001")
ENTITY_INSTANCE_ID = UUID("71000000-0000-0000-0000-000000000002")


class AlarmEventPublicApiTest(unittest.IsolatedAsyncioTestCase):
    def test_postgres_event_view_resolves_the_human_rule_name(self) -> None:
        from app.services.alarm_postgres import _alarm_rule_name

        self.assertEqual(
            "压缩机故障",
            _alarm_rule_name(
                f"alarm.pcs-fault-codes.{ENTITY_INSTANCE_ID}.e30",
                ENTITY_INSTANCE_ID,
                [
                    (
                        "pcs-fault-codes",
                        [
                            {"id": "e30", "name": "压缩机故障"},
                            {"id": "e42", "name": "直流母线过压"},
                        ],
                    )
                ],
            ),
        )

    async def test_open_event_list_excludes_a_cleared_pending_candidate(self) -> None:
        from app.api.alarm_events import list_alarm_events
        from app.services.alarm_runtime import (
            AlarmDefinition,
            AlarmObservation,
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )

        started_at = datetime(2026, 8, 14, 10, tzinfo=timezone.utc)
        definition = AlarmDefinition(
            id=DEFINITION_ID,
            asset_id="alarm.pcs.overpower",
            version="1.0.0",
            entity_instance_id=ENTITY_INSTANCE_ID,
            entity_definition_id="pcs.activePower",
            trigger={"op": "gt", "value": 100},
            trigger_duration_seconds=30,
            recovery={"op": "lte", "value": 90},
            recovery_duration_seconds=5,
            severity="MAJOR",
            notification_throttle_seconds=60,
        )
        runtime = AlarmRuntime(
            definitions=InMemoryAlarmDefinitionCatalog((definition,)),
            repository=InMemoryAlarmRepository(),
        )
        for offset, value in ((0, 101), (1, 80)):
            runtime.submit(
                AlarmObservation(
                    definition_id=DEFINITION_ID,
                    entity_instance_id=ENTITY_INSTANCE_ID,
                    observed_at=started_at + timedelta(seconds=offset),
                    value=value,
                    quality=192,
                    source_kind="entity",
                    source_ref="PCS-01.activePower",
                    evidence={"sample": offset},
                )
            )

        listed = await list_alarm_events(
            page=1,
            page_size=50,
            state="open",
            severity=None,
            entity_instance_id=None,
            runtime=runtime,
        )
        self.assertEqual([], listed["items"])
        self.assertEqual(0, listed["total"])
        self.assertEqual(0, listed["summary"]["active"])

    async def test_operator_reads_and_acknowledges_an_active_event_without_recovering_it(self) -> None:
        from app.api.alarm_events import get_alarm_runtime, router
        from app.services.alarm_runtime import (
            AlarmDefinition,
            AlarmObservation,
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )

        started_at = datetime(2026, 8, 14, 10, tzinfo=timezone.utc)
        runtime = AlarmRuntime(
            definitions=InMemoryAlarmDefinitionCatalog(
                (
                    AlarmDefinition(
                        id=DEFINITION_ID,
                        asset_id="alarm.pcs.overpower",
                        version="1.0.0",
                        entity_instance_id=ENTITY_INSTANCE_ID,
                        entity_definition_id="pcs.activePower",
                        trigger={"op": "gt", "value": 100},
                        trigger_duration_seconds=0,
                        recovery={"op": "lte", "value": 90},
                        recovery_duration_seconds=5,
                        severity="MAJOR",
                        notification_throttle_seconds=60,
                    ),
                )
            ),
            repository=InMemoryAlarmRepository(),
        )
        active = runtime.submit(
            AlarmObservation(
                definition_id=DEFINITION_ID,
                entity_instance_id=ENTITY_INSTANCE_ID,
                observed_at=started_at,
                value=101,
                quality=192,
                source_kind="entity",
                source_ref="PCS-01.activePower",
                evidence={
                    "sample": 1,
                    "node_name": "PCS-01",
                    "entity_name": "有功功率",
                    "alarm_name": "功率越限",
                },
            )
        )

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_alarm_runtime] = lambda: runtime
        async with AuthenticatedApiClient(app) as client:
            operator_headers = {"Authorization": await client._bearer("operator")}
            listed = await client._client.get(
                "/api/v1/alarm-events",
                headers=operator_headers,
            )
            self.assertEqual(200, listed.status_code, listed.text)
            self.assertEqual("v1", listed.json()["model_version"])
            self.assertEqual("active_unacknowledged", listed.json()["items"][0]["state"])
            self.assertEqual("v1", listed.json()["items"][0]["model_version"])
            self.assertEqual(1, listed.json()["total"])
            self.assertEqual("PCS-01", listed.json()["items"][0]["node_name"])
            self.assertEqual("有功功率", listed.json()["items"][0]["entity_name"])
            self.assertEqual("功率越限", listed.json()["items"][0]["alarm_name"])
            self.assertGreaterEqual(listed.json()["items"][0]["duration_seconds"], 0)
            self.assertNotIn("last_observation", listed.json()["items"][0])
            self.assertNotIn("definition_version", listed.json()["items"][0])
            self.assertEqual(1, listed.json()["summary"]["active"])
            self.assertEqual(1, listed.json()["summary"]["unacknowledged"])
            self.assertEqual(0, listed.json()["summary"]["critical"])

            acknowledged = await client._client.post(
                f"/api/v1/alarm-events/{active.event_id}/acknowledgements",
                headers=operator_headers,
                json={"note": "已知悉"},
            )
            self.assertEqual(200, acknowledged.status_code, acknowledged.text)
            body = acknowledged.json()
            self.assertEqual("active_acknowledged", body["state"])
            self.assertEqual("ALARM_ACKNOWLEDGED", body["code"])
            self.assertIsNotNone(body["audit_event_id"])
            UUID(body["audit_event_id"])

            event = await client._client.get(
                f"/api/v1/alarm-events/{active.event_id}",
                headers=operator_headers,
            )
            self.assertEqual(200, event.status_code, event.text)
            self.assertEqual("v1", event.json()["model_version"])
            self.assertEqual("active_acknowledged", event.json()["state"])
            self.assertEqual("user:00000000-0000-0000-0000-000000000003", event.json()["acknowledged_by"])

            transitions = await client._client.get(
                f"/api/v1/alarm-events/{active.event_id}/transitions",
                headers=operator_headers,
            )
            self.assertEqual(200, transitions.status_code, transitions.text)
            self.assertEqual("v1", transitions.json()["model_version"])
            self.assertEqual(
                ["pending", "active_unacknowledged", "active_acknowledged"],
                [item["to_state"] for item in transitions.json()["items"]],
            )

    async def test_recovered_history_does_not_inflate_current_alarm_summary(self) -> None:
        from app.api.alarm_events import list_alarm_events
        from app.services.alarm_runtime import (
            AlarmDefinition,
            AlarmObservation,
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )

        started_at = datetime(2026, 8, 14, 10, tzinfo=timezone.utc)
        runtime = AlarmRuntime(
            definitions=InMemoryAlarmDefinitionCatalog(
                (
                    AlarmDefinition(
                        id=DEFINITION_ID,
                        asset_id="alarm.pcs.overpower",
                        version="1.0.0",
                        entity_instance_id=ENTITY_INSTANCE_ID,
                        entity_definition_id="pcs.activePower",
                        trigger={"op": "gt", "value": 100},
                        trigger_duration_seconds=0,
                        recovery={"op": "lte", "value": 90},
                        recovery_duration_seconds=0,
                        severity="CRITICAL",
                        notification_throttle_seconds=60,
                    ),
                )
            ),
            repository=InMemoryAlarmRepository(),
        )
        for offset, value in ((0, 101), (1, 90)):
            runtime.submit(
                AlarmObservation(
                    definition_id=DEFINITION_ID,
                    entity_instance_id=ENTITY_INSTANCE_ID,
                    observed_at=started_at + timedelta(seconds=offset),
                    value=value,
                    quality=192,
                    source_kind="entity",
                    source_ref="PCS-01.activePower",
                    evidence={"sample": offset},
                )
            )

        listed = await list_alarm_events(
            page=1,
            page_size=50,
            state=None,
            severity=None,
            entity_instance_id=None,
            runtime=runtime,
        )

        self.assertEqual(1, listed["total"])
        self.assertEqual("recovered", listed["items"][0]["state"])
        self.assertEqual(
            {"active": 0, "unacknowledged": 0, "critical": 0},
            listed["summary"],
        )

    async def test_recovered_event_can_be_archived_and_remains_queryable_as_evidence(self) -> None:
        from app.api.alarm_events import get_alarm_runtime, router
        from app.services.alarm_runtime import (
            AlarmDefinition,
            AlarmObservation,
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )

        started_at = datetime(2026, 8, 14, 10, tzinfo=timezone.utc)
        runtime = AlarmRuntime(
            InMemoryAlarmDefinitionCatalog((AlarmDefinition(
                id=DEFINITION_ID,
                asset_id="alarm.pcs.overpower",
                version="1.0.0",
                entity_instance_id=ENTITY_INSTANCE_ID,
                entity_definition_id="pcs.activePower",
                trigger={"op": "gt", "value": 100},
                trigger_duration_seconds=0,
                recovery={"op": "lte", "value": 90},
                recovery_duration_seconds=0,
                severity="MAJOR",
                notification_throttle_seconds=60,
            ),)),
            InMemoryAlarmRepository(),
        )
        activated = runtime.submit(AlarmObservation(
            DEFINITION_ID, ENTITY_INSTANCE_ID, started_at, 101, 192,
            "entity", "PCS-01.activePower", {"sample": 1},
        ))
        runtime.submit(AlarmObservation(
            DEFINITION_ID, ENTITY_INSTANCE_ID, started_at + timedelta(seconds=1),
            90, 192, "entity", "PCS-01.activePower", {"sample": 2},
        ))

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_alarm_runtime] = lambda: runtime
        async with AuthenticatedApiClient(app) as client:
            headers = {"Authorization": await client._bearer("engineer")}
            archived = await client._client.post(
                f"/api/v1/alarm-events/{activated.event_id}/archivations",
                headers=headers,
            )
            current = await client._client.get(
                "/api/v1/alarm-events?state=recovered",
                headers=headers,
            )
            evidence = await client._client.get(
                "/api/v1/alarm-events?state=archived",
                headers=headers,
            )

        self.assertEqual(200, archived.status_code, archived.text)
        self.assertIsNotNone(archived.json()["archived_at"])
        self.assertEqual(0, current.json()["total"])
        self.assertEqual(1, evidence.json()["total"])
        self.assertEqual(activated.event_id.hex, evidence.json()["items"][0]["id"].replace("-", ""))

    async def test_active_event_cannot_be_archived(self) -> None:
        from app.api.alarm_events import get_alarm_runtime, router
        from app.services.alarm_runtime import (
            AlarmDefinition,
            AlarmObservation,
            AlarmRuntime,
            InMemoryAlarmDefinitionCatalog,
            InMemoryAlarmRepository,
        )

        runtime = AlarmRuntime(
            InMemoryAlarmDefinitionCatalog((AlarmDefinition(
                id=DEFINITION_ID,
                asset_id="alarm.pcs.overpower",
                version="1.0.0",
                entity_instance_id=ENTITY_INSTANCE_ID,
                entity_definition_id="pcs.activePower",
                trigger={"op": "gt", "value": 100},
                trigger_duration_seconds=0,
                recovery={"op": "lte", "value": 90},
                recovery_duration_seconds=0,
                severity="MAJOR",
                notification_throttle_seconds=60,
            ),)),
            InMemoryAlarmRepository(),
        )
        active = runtime.submit(AlarmObservation(
            DEFINITION_ID, ENTITY_INSTANCE_ID,
            datetime(2026, 8, 14, 10, tzinfo=timezone.utc), 101, 192,
            "entity", "PCS-01.activePower", {"sample": 1},
        ))

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_alarm_runtime] = lambda: runtime
        async with AuthenticatedApiClient(app) as client:
            response = await client._client.post(
                f"/api/v1/alarm-events/{active.event_id}/archivations",
                headers={"Authorization": await client._bearer("engineer")},
            )

        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("ALARM_EVENT_ARCHIVE_NOT_ALLOWED", response.json()["detail"]["code"])


if __name__ == "__main__":
    unittest.main()
