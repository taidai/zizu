"""Internal configuration revision contract shared by configuration publishers."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import re
from threading import Condition
import time
from typing import Any

from app.services.data_trunk_contracts import (
    BlackboardState,
    DataTrunkError,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
CONFIGURATION_DRAIN_TIMEOUT_SECONDS = 30.0


class ConfigurationRevisionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class GateState(str, Enum):
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    CLOSING = "CLOSING"
    QUIESCED = "QUIESCED"


@dataclass(frozen=True)
class RuntimeRevision:
    revision: int
    blackboard_state: BlackboardState


class ConfigurationRuntimeGate:
    """Drain old frames before switching the L1 configuration revision."""

    def __init__(self, repository: Any, blackboard: Any) -> None:
        self._repository = repository
        self._blackboard = blackboard
        self._condition = Condition()
        self._state = GateState.RUNNING
        self._capture_inflight = 0
        self._processor_inflight = 0
        self._consumer_registered = False

    @property
    def state(self) -> GateState:
        with self._condition:
            return self._state

    def register_committed_frame_consumer(self) -> None:
        with self._condition:
            self._consumer_registered = True
            self._condition.notify_all()

    def enter_capture(self) -> bool:
        with self._condition:
            if self._state is not GateState.RUNNING:
                return False
            self._capture_inflight += 1
            return True

    def leave_capture(self) -> None:
        with self._condition:
            self._capture_inflight -= 1
            self._condition.notify_all()

    def enter_processor(self) -> bool:
        with self._condition:
            if self._state not in {GateState.RUNNING, GateState.DRAINING}:
                return False
            self._processor_inflight += 1
            return True

    def leave_processor(self) -> None:
        with self._condition:
            self._processor_inflight -= 1
            self._condition.notify_all()

    def begin_configuration_publish(
        self,
        base_revision: int,
        *,
        timeout_seconds: float = CONFIGURATION_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        with self._condition:
            if not self._consumer_registered:
                raise DataTrunkError(
                    "COMMITTED_FRAME_CONSUMER_MISSING",
                    "COMMITTED_FRAME_CONSUMER_MISSING",
                )
            if self._state is not GateState.RUNNING:
                raise DataTrunkError(
                    "CONFIGURATION_RUNTIME_BUSY",
                    "CONFIGURATION_RUNTIME_BUSY",
                )
            if self._repository.current_configuration_revision() != base_revision:
                raise DataTrunkError(
                    "DATA_FRAME_CONFIGURATION_STALE",
                    "DATA_FRAME_CONFIGURATION_STALE",
                )
            self._state = GateState.DRAINING

        deadline = time.monotonic() + timeout_seconds
        while True:
            with self._condition:
                inflight = self._capture_inflight + self._processor_inflight
            unfinished = self._repository.unfinished_frame_count()
            unpublished = self._repository.unpublished_frame_outbox_count()
            if inflight == 0 and unfinished == 0 and unpublished == 0:
                with self._condition:
                    self._state = GateState.CLOSING
                if (
                    self._repository.unfinished_frame_count() == 0
                    and self._repository.unpublished_frame_outbox_count() == 0
                ):
                    with self._condition:
                        if self._capture_inflight == 0 and self._processor_inflight == 0:
                            self._state = GateState.QUIESCED
                            return
                with self._condition:
                    self._state = GateState.DRAINING
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._condition:
                    self._state = GateState.RUNNING
                    self._condition.notify_all()
                raise DataTrunkError(
                    "CONFIGURATION_RUNTIME_DRAIN_TIMEOUT",
                    "CONFIGURATION_RUNTIME_DRAIN_TIMEOUT",
                )
            with self._condition:
                self._condition.wait(timeout=min(0.05, remaining))

    def cancel_configuration_publish(self) -> None:
        with self._condition:
            if self._state is GateState.QUIESCED:
                self._state = GateState.RUNNING
                self._condition.notify_all()

    def reconcile_configuration_runtime(self) -> RuntimeRevision:
        with self._condition:
            if self._state is not GateState.QUIESCED:
                raise DataTrunkError(
                    "CONFIGURATION_RUNTIME_NOT_QUIESCED",
                    "CONFIGURATION_RUNTIME_NOT_QUIESCED",
                )
        try:
            recovery = self._repository.restore_blackboard()
            self._blackboard.reset_revision(
                recovery.configuration_revision,
                recovery.active_input_contracts,
                recovery.required_tag_ids,
            )
        except Exception as exc:
            raise DataTrunkError(
                "CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED",
                "CONFIGURATION_RUNTIME_RECONCILIATION_REQUIRED",
            ) from exc
        with self._condition:
            self._state = GateState.RUNNING
            self._condition.notify_all()
        return RuntimeRevision(
            revision=recovery.configuration_revision,
            blackboard_state=self._blackboard.state,
        )


def validate_configuration_publish(
    *,
    base_revision: int,
    actor: str,
    action: str,
    resource_kind: str,
    resource_id: str,
    before_digest: str | None,
    after_digest: str,
    details: Mapping[str, Any],
) -> None:
    if base_revision < 0:
        raise ConfigurationRevisionError(
            "CONFIGURATION_REVISION_INVALID", "Base revision must be non-negative"
        )
    for name, value in (
        ("actor", actor),
        ("action", action),
        ("resource_kind", resource_kind),
        ("resource_id", resource_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationRevisionError(
                "CONFIGURATION_REVISION_INVALID", f"{name} is required"
            )
    if not _DIGEST.fullmatch(after_digest) or (
        before_digest is not None and not _DIGEST.fullmatch(before_digest)
    ):
        raise ConfigurationRevisionError(
            "CONFIGURATION_REVISION_INVALID", "Configuration digests must be SHA-256"
        )
    if not isinstance(details, Mapping):
        raise ConfigurationRevisionError(
            "CONFIGURATION_REVISION_INVALID", "details must be a mapping"
        )


__all__ = [
    "CONFIGURATION_DRAIN_TIMEOUT_SECONDS",
    "ConfigurationRevisionError",
    "validate_configuration_publish",
]
