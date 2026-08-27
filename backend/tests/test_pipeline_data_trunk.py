from __future__ import annotations

import inspect
import json
import os
from types import SimpleNamespace
import unittest
from uuid import UUID

os.environ.setdefault("DB_PASSWORD", "database-secret-value")
os.environ.setdefault("NEURON_PASSWORD", "neuron-secret-value")
os.environ.setdefault("NANOMQ_API_PASSWORD", "nanomq-secret-value")
os.environ.setdefault("JWT_SECRET", "jwt-secret-value-that-is-at-least-32-chars")

from app.services.data_trunk_contracts import AcceptReceipt, TypedValue
from app.services.normalizer import TagNormalizationRule
from app.services.pipeline import DataPipeline


NODE_ID = UUID("41000000-0000-0000-0000-000000000001")
TAG_ID = UUID("41000000-0000-0000-0000-000000000002")


class _RecordingFrameTrunk:
    def __init__(self) -> None:
        self.accepted = []
        self.capture_calls = 0

    def accept(self, observations):
        self.accepted.extend(observations)
        return AcceptReceipt(len(observations), 0)

    def capture_tick(self, _now):
        self.capture_calls += 1

    def close(self):
        return None


class PipelineDataTrunkTest(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_only_accepts_canonical_l0_into_blackboard(self) -> None:
        trunk = _RecordingFrameTrunk()
        pipeline = DataPipeline(data_trunk=trunk)
        rule = TagNormalizationRule(
            tag_name="activePower",
            data_type="FLOAT",
            scale_factor=0.001,
            unit_from="W",
            unit_to="kW",
        )
        pipeline._rules = {"activePower": rule}
        pipeline._neuron_tag_map = {
            ("PCS-A", "read", "activePower"): (NODE_ID, TAG_ID, rule)
        }

        await pipeline.on_message(
            SimpleNamespace(
                topic="neuron/PCS-A/telemetry",
                qos=1,
                sequence=9,
                payload=json.dumps(
                    {
                        "node": "PCS-A",
                        "group": "read",
                        "timestamp": 1786932000000,
                        "tags": {"activePower": 12345},
                    }
                ).encode(),
            )
        )

        self.assertEqual((9,), tuple(item.source_sequence for item in trunk.accepted))
        self.assertEqual(TypedValue.float(12345.0), trunk.accepted[0].value)
        self.assertEqual(0, trunk.capture_calls)

    def test_pipeline_has_no_database_or_legacy_alarm_write_path(self) -> None:
        source = inspect.getsource(DataPipeline)
        self.assertNotIn("self._buffer", source)
        self.assertNotIn("flush_now", source)
        self.assertNotIn("record_failure", source)
        self.assertNotIn("self._data_trunk.ingest", source)
        self.assertNotIn("_submit_installed_entity_alarms", source)
        self.assertEqual(1, source.count("self._data_trunk.accept"))


if __name__ == "__main__":
    unittest.main()
