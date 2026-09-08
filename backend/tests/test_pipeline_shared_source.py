from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch
import unittest
from uuid import UUID

from test_pipeline_same_value_freshness import _CaptureRepository, _message, NODE_ID, TAG_ID, NOW
from app.services.data_trunk import DataTrunk
from app.services.data_trunk_contracts import SourceOrderMode, TrunkQuality
from app.services.pipeline import DataPipeline
from app.services.realtime_blackboard import RealtimeBlackboard

SECOND_NODE = UUID('89000000-0000-0000-0000-000000000003')
SECOND_TAG = UUID('89000000-0000-0000-0000-000000000004')


class SharedSourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_enabled_import_of_same_source_receives_new_samples(self):
        # A single-value source map silently starves one of two node-owned L0s.
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                repository = _CaptureRepository()
                trunk = DataTrunk(repository, blackboard=RealtimeBlackboard(
                    active_input_contracts={tag: SourceOrderMode.OBSERVED_AT for tag in (TAG_ID, SECOND_TAG)},
                    required_tag_ids=frozenset({TAG_ID, SECOND_TAG}),
                ), processor=None)
                pipeline = DataPipeline(data_trunk=trunk)
                rows = [
                    ('timeout', name, tag, node, 'INT', 1, 0, 's', 's', None, None,
                     'neuron', 'PCS/read/timeout', 'INT16', None, True, False)
                    for name, tag, node in [('first', TAG_ID, NODE_ID), ('second', SECOND_TAG, SECOND_NODE)]
                ]
                connection = MagicMock()
                connection.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = rows[::-1] if reverse else rows
                with patch('app.services.telemetry_store.get_connection', return_value=connection):
                    await pipeline._load_tag_rules()
                for second in (0, 1):
                    await pipeline.on_message(_message(second))
                    self.assertIsNotNone(trunk.capture_tick(NOW + timedelta(seconds=second)))
                    frame = repository.candidates[-1]
                    self.assertEqual({TAG_ID, SECOND_TAG}, set(frame.cells))
                    self.assertEqual(2, len(frame.changed_l0))
                    for tag in (TAG_ID, SECOND_TAG):
                        self.assertEqual(100, frame.cells[tag].observation.value.value)
                        self.assertEqual(TrunkQuality.GOOD, frame.cells[tag].effective_quality)
                    self.assertNotEqual(frame.cells[TAG_ID].observation.observation_id, frame.cells[SECOND_TAG].observation.observation_id)
                await pipeline.on_message(_message(0, value=999))
                self.assertIsNone(trunk.capture_tick(NOW + timedelta(seconds=2)))


if __name__ == '__main__':
    unittest.main()
