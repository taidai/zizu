from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import UUID

from app.services.data_trunk import DataTrunk, InMemoryDataTrunkRepository
from app.services.data_trunk_contracts import (
    InstalledPointConversion,
    RawObservation,
    TrunkQuality,
    TypedValue,
)


class InMemoryDataTrunkTest(unittest.TestCase):
    def test_commits_l2_after_l0_and_deduplicates_replayed_source(self) -> None:
        raw = RawObservation(
            observation_id=UUID("00000000-0000-0000-0000-000000000101"),
            node_id=UUID("00000000-0000-0000-0000-000000000001"),
            tag_id=UUID("00000000-0000-0000-0000-000000000011"),
            source_key="ActivePowerRaw",
            value=TypedValue.float(20_000.0),
            raw_unit="W",
            quality=TrunkQuality.GOOD,
            source_timestamp=datetime(2026, 8, 17, tzinfo=UTC),
            received_at=datetime(2026, 8, 17, 0, 0, 1, tzinfo=UTC),
            source_message_id="message-digest",
            source_sequence=None,
            source_digest="a" * 64,
        )
        installed = InstalledPointConversion.numeric(
            installation_id=UUID("00000000-0000-0000-0000-000000000201"),
            revision_id=UUID("00000000-0000-0000-0000-000000000202"),
            input_tag_id=raw.tag_id,
            output_entity_instance_id=UUID(
                "00000000-0000-0000-0000-000000000301"
            ),
            output_definition_id="pcs.active_power",
            scale=0.001,
            offset=0.0,
            input_unit="W",
            output_unit="kW",
            minimum=-500.0,
            maximum=500.0,
        )
        committed = []
        repository = InMemoryDataTrunkRepository(
            installed_provider=lambda: (installed,),
            site_configuration_version=lambda: 7,
            on_l2_committed=committed.extend,
            clock=lambda: datetime(2026, 8, 17, 0, 0, 2, tzinfo=UTC),
        )
        trunk = DataTrunk(repository)

        first = trunk.ingest((raw,))
        replay = trunk.ingest((raw,))

        self.assertEqual(
            (first.accepted_l0_count, first.duplicate_l0_count),
            (1, 0),
        )
        self.assertEqual(
            (replay.accepted_l0_count, replay.duplicate_l0_count),
            (0, 1),
        )
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0].value, TypedValue.float(20.0))
        self.assertEqual(committed[0].site_configuration_version, 7)
        self.assertEqual(first.l2_event_ids, (committed[0].event_id,))
        self.assertEqual(replay.l2_event_ids, ())


if __name__ == "__main__":
    unittest.main()
