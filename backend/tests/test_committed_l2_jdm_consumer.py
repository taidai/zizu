"""Behavior tests for the committed-L2 JDM consumer."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import unittest
from uuid import UUID, uuid4

from app.services.data_trunk_contracts import (
    FrameStatus,
    TrunkQuality,
    TypedValue,
)
from app.services.data_trunk_outbox import (
    CommittedL2Change,
    FrameOutboxEvent,
)
from app.services.committed_l2_jdm_consumer import CommittedL2JdmConsumer
from app.services.jdm_runtime import JdmModel, JdmRuntime, evaluate_model


NOW = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)
SOURCE_ID = UUID("a1000000-0000-0000-0000-000000000001")
TARGET_ID = UUID("a1000000-0000-0000-0000-000000000002")


def _change(
    entity_id: UUID = SOURCE_ID,
    *,
    value: float = 12.0,
    quality: TrunkQuality = TrunkQuality.GOOD,
    observed_at: datetime | None = NOW,
) -> CommittedL2Change:
    return CommittedL2Change(
        entity_instance_id=entity_id,
        event_id=uuid4(),
        value=TypedValue.float(value),
        quality=quality,
        reason=None,
        node_id=uuid4(),
        unit="kW",
        observed_at=observed_at,
        received_at=NOW,
        calculated_at=NOW,
        processing_revision_id=uuid4(),
        source_digest="a" * 64,
    )


def _frame(
    *changes: CommittedL2Change,
    revision: int = 7,
    sequence: int = 7001,
) -> FrameOutboxEvent:
    return FrameOutboxEvent(
        frame_id=uuid4(),
        frame_sequence=sequence,
        status=FrameStatus.COMPLETE,
        configuration_revision=revision,
        l0_changes=(),
        l2_changes=tuple(changes),
        failure_id=None,
        failure_code=None,
        frame_time=NOW,
    )


def _model(
    *,
    rule_id: UUID | None = None,
    revision: int = 7,
    when: str = "power > 10",
    source_id: UUID = SOURCE_ID,
) -> JdmModel:
    return JdmModel(
        id=rule_id or uuid4(),
        version=2,
        configuration_revision=revision,
        content={
            "when": when,
            "_config": {
                "inputMappings": {"power": str(source_id)},
                "sourceEntityInstanceIds": [str(source_id)],
                "actions": [
                    {
                        "id": "set-limit",
                        "type": "control",
                        "entity_instance_id": str(TARGET_ID),
                        "value": 5,
                    }
                ],
            },
        },
    )


class _Transaction:
    def __init__(self, repository: "_Repository") -> None:
        self._repository = repository
        self.receipts = set(repository.receipts)
        self.executions = list(repository.executions)

    def begin_committed_frame(
        self,
        consumer_key: str,
        frame_id: UUID,
        frame_sequence: int,
        configuration_revision: int,
    ) -> bool:
        receipt = (consumer_key, frame_id, frame_sequence, configuration_revision)
        if any(item[:2] == receipt[:2] for item in self.receipts):
            return False
        self.receipts.add(receipt)
        return True

    def active_models(self, configuration_revision: int) -> tuple[JdmModel, ...]:
        return tuple(
            model
            for model in self._repository.models
            if model.configuration_revision <= configuration_revision
        )

    def save_execution(self, execution) -> None:
        if (
            self._repository.fail_on_save_number is not None
            and len(self.executions) + 1
            == self._repository.fail_on_save_number
        ):
            raise RuntimeError("simulated execution persistence failure")
        self.executions.append(execution)


class _Repository:
    def __init__(
        self,
        *models: JdmModel,
        fail_on_save_number: int | None = None,
    ) -> None:
        self.models = tuple(models)
        self.fail_on_save_number = fail_on_save_number
        self.receipts: set[tuple[str, UUID, int, int]] = set()
        self.executions = []

    @contextmanager
    def transaction(self):
        transaction = _Transaction(self)
        try:
            yield transaction
        except Exception:
            raise
        else:
            self.receipts = transaction.receipts
            self.executions = transaction.executions


def _consumer(repository: _Repository) -> CommittedL2JdmConsumer:
    return CommittedL2JdmConsumer(JdmRuntime(repository))


class CommittedL2JdmConsumerTest(unittest.IsolatedAsyncioTestCase):
    async def test_good_committed_l2_records_judgment_and_control_intent(self) -> None:
        repository = _Repository(_model())
        event = _frame(_change())

        await _consumer(repository).publish(event)

        self.assertEqual(1, len(repository.executions))
        execution = repository.executions[0]
        self.assertEqual("executed", execution.status)
        self.assertIsNone(execution.reason_code)
        self.assertEqual(event.frame_id, execution.frame_id)
        self.assertEqual(event.frame_sequence, execution.frame_sequence)
        self.assertEqual(7, execution.configuration_revision)
        self.assertEqual(192, execution.inputs["power"]["quality"])
        self.assertEqual(12.0, execution.inputs["power"]["value"])
        self.assertEqual(
            [
                {
                    "id": "set-limit",
                    "type": "control",
                    "entity_instance_id": str(TARGET_ID),
                    "value": 5,
                }
            ],
            execution.actions,
        )

    async def test_missing_bad_or_untimed_input_records_stable_rejection(self) -> None:
        cases = (
            ((), "JDM_INPUT_MISSING"),
            ((_change(quality=TrunkQuality.STALE),), "JDM_INPUT_QUALITY_NOT_GOOD"),
            ((_change(observed_at=None),), "JDM_INPUT_TIMESTAMP_MISSING"),
        )
        for changes, expected in cases:
            with self.subTest(expected=expected):
                repository = _Repository(_model())
                await _consumer(repository).publish(_frame(*changes))
                self.assertEqual(expected, repository.executions[0].reason_code)
                self.assertEqual("rejected", repository.executions[0].status)

    async def test_future_model_revision_is_rejected_without_evaluation(self) -> None:
        repository = _Repository(_model(revision=8))
        event = _frame(_change(), revision=7)
        repository.models = (_model(revision=8),)

        execution = evaluate_model(repository.models[0], event)

        self.assertEqual("rejected", execution.status)
        self.assertEqual(
            "JDM_MODEL_CONFIGURATION_MISMATCH",
            execution.reason_code,
        )

    async def test_evaluation_error_is_a_fail_closed_execution_fact(self) -> None:
        repository = _Repository(_model(when="power >>> 10"))

        await _consumer(repository).publish(_frame(_change()))

        self.assertEqual("rejected", repository.executions[0].status)
        self.assertEqual(
            "JDM_EVALUATION_FAILED",
            repository.executions[0].reason_code,
        )

    async def test_invalid_model_input_mapping_is_recorded_not_raised(self) -> None:
        model = _model()
        model.content["_config"]["inputMappings"]["power"] = "not-an-entity-id"
        repository = _Repository(model)

        await _consumer(repository).publish(_frame(_change()))

        self.assertEqual("rejected", repository.executions[0].status)
        self.assertEqual("JDM_MODEL_INVALID", repository.executions[0].reason_code)

    async def test_same_frame_replay_is_a_noop_after_atomic_commit(self) -> None:
        repository = _Repository(_model())
        event = _frame(_change())
        consumer = _consumer(repository)

        await consumer.publish(event)
        await consumer.publish(event)

        self.assertEqual(1, len(repository.receipts))
        self.assertEqual(1, len(repository.executions))

    async def test_second_save_failure_rolls_back_receipt_and_first_execution(self) -> None:
        repository = _Repository(
            _model(rule_id=uuid4()),
            _model(rule_id=uuid4()),
            fail_on_save_number=2,
        )
        event = _frame(_change())

        with self.assertRaisesRegex(RuntimeError, "persistence failure"):
            await _consumer(repository).publish(event)

        self.assertEqual(set(), repository.receipts)
        self.assertEqual([], repository.executions)

        repository.fail_on_save_number = None
        await _consumer(repository).publish(event)
        self.assertEqual(1, len(repository.receipts))
        self.assertEqual(2, len(repository.executions))


if __name__ == "__main__":
    unittest.main()
