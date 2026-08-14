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

from tests.test_delivery_public_api import AuthenticatedDeliveryClient


DEFINITION_ID = UUID("71000000-0000-0000-0000-000000000001")
ENTITY_INSTANCE_ID = UUID("71000000-0000-0000-0000-000000000002")


class AlarmEventPublicApiTest(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(0, listed["summary"]["total"])

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
                evidence={"sample": 1},
            )
        )

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_alarm_runtime] = lambda: runtime
        async with AuthenticatedDeliveryClient(app) as client:
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
            self.assertEqual(1, listed.json()["summary"]["unacknowledged"])
            self.assertEqual(1, listed.json()["summary"]["by_severity"]["MAJOR"])

            acknowledged = await client._client.post(
                f"/api/v1/alarm-events/{active.event_id}/acknowledgements",
                headers=operator_headers,
                json={"note": "已知悉"},
            )
            self.assertEqual(200, acknowledged.status_code, acknowledged.text)
            body = acknowledged.json()
            self.assertEqual("active_acknowledged", body["state"])
            self.assertEqual("ALARM_ACKNOWLEDGED", body["code"])

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


if __name__ == "__main__":
    unittest.main()
