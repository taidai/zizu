from __future__ import annotations

import time
from typing import Any, Protocol
from uuid import UUID


class DataTrunkEvidenceReader(Protocol):
    def acceptance_evidence(
        self,
        *,
        solution_installation_id: UUID,
        entity_definition_ids: tuple[str, ...],
    ) -> dict[str, Any]: ...


class DataTrunkAcceptance:
    """Classify committed PCS data-trunk evidence without mutating it."""

    def __init__(self, evidence_reader: DataTrunkEvidenceReader) -> None:
        self._evidence_reader = evidence_reader

    def evaluate(
        self,
        *,
        installation_id: UUID,
        acceptance_id: str,
        definition: dict[str, Any],
        started_at: float,
    ) -> dict[str, Any]:
        required_definitions = tuple(sorted(definition["entityDefinitions"]))
        snapshot = self._evidence_reader.acceptance_evidence(
            solution_installation_id=installation_id,
            entity_definition_ids=required_definitions,
        )
        observed = set(snapshot.get("observed_entity_definitions", ()))
        required = set(required_definitions)
        entity_count = len(set(snapshot.get("entity_instance_ids", ())))
        committed_count = int(snapshot.get("committed_event_count", 0))
        outbox_count = int(snapshot.get("outbox_event_count", committed_count))
        latest_count = int(snapshot.get("l2_latest_count", entity_count))
        source_count = int(snapshot.get("source_observation_count", 0))
        checks = {
            "l0_source_lineage_and_l2_latest": (
                source_count > 0 and latest_count >= len(required)
            ),
            "declared_outputs_observed": required.issubset(observed),
            "quality_and_timestamps": (
                int(snapshot.get("good_latest_count", 0)) >= len(required)
                and int(snapshot.get("ordered_timestamp_count", 0)) >= len(required)
            ),
            "committed_outbox": outbox_count >= len(required),
        }
        requested_checks = tuple(definition["checks"])
        passed = all(checks.get(name, False) for name in requested_checks)
        public_evidence = {
            "checks": {name: checks.get(name, False) for name in requested_checks},
            "entity_definitions": list(required_definitions),
            "entity_instance_ids": sorted(
                str(item) for item in snapshot.get("entity_instance_ids", ())
            ),
            "processing_revision_ids": sorted(
                str(item) for item in snapshot.get("processing_revision_ids", ())
            ),
            "site_configuration_versions": sorted(
                int(item) for item in snapshot.get("site_configuration_versions", ())
            ),
            "l0_observation_count": int(snapshot.get("l0_observation_count", 0)),
            "l2_observation_count": int(snapshot.get("l2_observation_count", 0)),
            "committed_event_count": committed_count,
        }
        return {
            "acceptance_id": acceptance_id,
            "status": "passed" if passed else "failed",
            "code": (
                "DATA_TRUNK_VERIFIED" if passed else "DATA_TRUNK_EVIDENCE_INCOMPLETE"
            ),
            "required": bool(definition.get("required", True)),
            "duration_ms": max(0, round((time.monotonic() - started_at) * 1000)),
            "evidence": public_evidence,
        }
