from __future__ import annotations

import unittest
from uuid import UUID

from app.services.data_trunk_acceptance import DataTrunkAcceptance


INSTALLATION_ID = UUID("93000000-0000-0000-0000-000000000001")


class _Evidence:
    def acceptance_evidence(self, **_kwargs):
        return {
            "observed_entity_definitions": [
                "pcs.active_power",
                "pcs.operating_state",
                "pcs.fault_codes",
            ],
            "entity_instance_ids": [
                "93000000-0000-0000-0000-000000000011",
                "93000000-0000-0000-0000-000000000012",
                "93000000-0000-0000-0000-000000000013",
            ],
            "processing_revision_ids": [
                "93000000-0000-0000-0000-000000000021",
            ],
            "site_configuration_versions": [1],
            "l0_observation_count": 3,
            "l2_observation_count": 3,
            "l2_latest_count": 3,
            "source_observation_count": 3,
            "committed_event_count": 3,
            "outbox_event_count": 3,
            "good_latest_count": 3,
            "ordered_timestamp_count": 3,
        }


class DataTrunkAcceptanceTest(unittest.TestCase):
    def test_report_contains_only_facts_observable_from_committed_runtime(self) -> None:
        definition = {
            "required": True,
            "entityDefinitions": [
                "pcs.active_power",
                "pcs.operating_state",
                "pcs.fault_codes",
            ],
            "checks": [
                "l0_source_lineage_and_l2_latest",
                "declared_outputs_observed",
                "quality_and_timestamps",
                "committed_outbox",
            ],
        }

        result = DataTrunkAcceptance(_Evidence()).evaluate(
            installation_id=INSTALLATION_ID,
            acceptance_id="acceptance.pcs-data-trunk",
            definition=definition,
            started_at=0,
        )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            set(result["evidence"]["checks"]),
            set(definition["checks"]),
        )
        self.assertNotIn("restart_persistence", result["evidence"]["checks"])
        self.assertNotIn(
            "atomic_rollback_and_idempotency",
            result["evidence"]["checks"],
        )
        self.assertNotIn("committed_websocket", result["evidence"]["checks"])
        self.assertNotIn("brand_replacement_identity", result["evidence"]["checks"])

    def test_missing_good_current_values_fails_quality_check(self) -> None:
        class BadQualityEvidence(_Evidence):
            def acceptance_evidence(self, **kwargs):
                evidence = super().acceptance_evidence(**kwargs)
                evidence["good_latest_count"] = 2
                return evidence

        result = DataTrunkAcceptance(BadQualityEvidence()).evaluate(
            installation_id=INSTALLATION_ID,
            acceptance_id="acceptance.pcs-data-trunk",
            definition={
                "required": True,
                "entityDefinitions": [
                    "pcs.active_power",
                    "pcs.operating_state",
                    "pcs.fault_codes",
                ],
                "checks": ["quality_and_timestamps"],
            },
            started_at=0,
        )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["evidence"]["checks"]["quality_and_timestamps"])


if __name__ == "__main__":
    unittest.main()
