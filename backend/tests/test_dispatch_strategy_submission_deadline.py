from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.dispatch_strategy_postgres import PostgresStrategyRepository, _json_safe
from app.services.dispatch_strategies import EntityBindingContract, StrategyBindingDraft
from tests.test_dispatch_strategy_runtime import NOW, OUTPUT_ID, SOC_ID, _revision, _snapshot


class DispatchStrategySubmissionDeadlineTest(unittest.TestCase):
    def _guard(self, *, delay=0, current_age=0, persisted_age=0, duplicate_binding=False, extra_current=False):
        revision = _revision()
        if duplicate_binding:
            bindings = tuple(
                replace(item, freshness_seconds=30.0) if item.direction == "OUTPUT" else item
                for item in revision.bindings
            )
            revision = replace(revision, bindings=(*bindings, StrategyBindingDraft(
                "INPUT", "soc-copy", 1, SOC_ID, "FLOAT", "%", 20.0,
            )))
        snapshot = _snapshot()
        persisted = replace(snapshot, inputs=tuple(
            replace(item, observed_at=NOW - timedelta(seconds=persisted_age))
            for item in snapshot.inputs
        ))
        evidence = _json_safe(asdict(persisted))
        # Caller-controlled evidence never declares the actual write deadline.
        evidence["source_fresh_until"] = (NOW + timedelta(days=1)).isoformat()
        for item in evidence["inputs"]:
            item["binding_key"] = item.pop("field_key")
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (7,), (True, revision.id, "READY"), ("IN_FLIGHT", 1, None),
            (1,), ("revision row",),
        ]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        self.clock = NOW
        repository = PostgresStrategyRepository(
            connection_factory=lambda: nullcontext(connection), clock=lambda: self.clock,
        )
        repository._write = lambda: nullcontext(connection)
        repository._lock_strategy = lambda *args: None
        repository._revision_from_row = lambda *args: revision
        repository._load_entity_contracts = lambda *args: {
            SOC_ID: EntityBindingContract(True, "FLOAT", "%", "R", 0, None, None, "bms.soc"),
            OUTPUT_ID: EntityBindingContract(True, "FLOAT", "kW", "RW", 1, 0, 200, "pcs.setpoint"),
        }

        def delayed_snapshot(model, frame, now):
            self.clock += timedelta(seconds=delay)
            inputs = tuple(
                replace(item, observed_at=NOW - timedelta(seconds=current_age))
                for item in snapshot.inputs
            )
            if extra_current:
                inputs = (*inputs, replace(
                    snapshot.inputs[0], entity_instance_id=uuid4(), field_key="unrelated",
                    observed_at=NOW - timedelta(days=1),
                ))
            return replace(snapshot, evaluated_at=now, inputs=inputs)

        repository.load_snapshot = delayed_snapshot
        intent = SimpleNamespace(
            id=uuid4(), strategy_id=revision.strategy_id, revision_id=revision.id,
            attempt_count=1, entity_instance_id=OUTPUT_ID, snapshot_evidence=evidence,
            evaluation_key="test:submission-deadline",
        )
        with repository.submission_guard(intent, NOW) as deadline:
            return deadline, cursor

    def test_slow_snapshot_loading_rechecks_current_clock_before_authorizing_write(self):
        deadline, cursor = self._guard(delay=11)
        self.assertIsNone(deadline)
        self.assertEqual("L2_INPUT_STALE", cursor.execute.call_args.args[1][0])

    def test_deadline_uses_earliest_current_or_persisted_observation_not_evidence_claim(self):
        for current_age, persisted_age in ((2, 0), (0, 3)):
            with self.subTest(current_age=current_age, persisted_age=persisted_age):
                deadline, _ = self._guard(current_age=current_age, persisted_age=persisted_age)
                self.assertEqual(NOW + timedelta(seconds=10 - max(current_age, persisted_age)), deadline)

    def test_freshness_boundary_remains_inclusive_but_one_microsecond_late_blocks(self):
        deadline, _ = self._guard(delay=10)
        self.assertEqual(NOW + timedelta(seconds=10), deadline)
        deadline, _ = self._guard(delay=10.000001)
        self.assertIsNone(deadline)

    def test_looser_alias_for_same_entity_cannot_extend_strict_binding_deadline(self):
        deadline, _ = self._guard(duplicate_binding=True)
        self.assertEqual(NOW + timedelta(seconds=10), deadline)

    def test_unrelated_snapshot_sample_does_not_change_binding_deadline(self):
        deadline, _ = self._guard(extra_current=True)
        self.assertEqual(NOW + timedelta(seconds=10), deadline)
