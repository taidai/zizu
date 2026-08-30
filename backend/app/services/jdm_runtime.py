"""Evaluate published JDM models from one immutable committed-L2 frame."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from app.services.data_trunk_contracts import TrunkQuality
from app.services.data_trunk_outbox import FrameOutboxEvent
from app.services.gorules_adapter import evaluate_rule


@dataclass(frozen=True)
class JdmModel:
    id: UUID
    version: int
    configuration_revision: int
    content: dict[str, Any]


@dataclass(frozen=True)
class JdmExecution:
    id: UUID
    rule_id: UUID
    rule_version: int
    frame_id: UUID
    frame_sequence: int
    configuration_revision: int
    model_digest: str
    status: str
    reason_code: str | None
    inputs: dict[str, dict[str, Any]]
    outputs: dict[str, Any]
    actions: list[dict[str, Any]]


class JdmRuntimeError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class JdmTransaction(Protocol):
    def begin_committed_frame(
        self,
        consumer_key: str,
        frame_id: UUID,
        frame_sequence: int,
        configuration_revision: int,
    ) -> bool: ...

    def active_models(
        self,
        configuration_revision: int,
    ) -> tuple[JdmModel, ...]: ...

    def save_execution(self, execution: JdmExecution) -> None: ...


class JdmRepository(Protocol):
    def transaction(self) -> AbstractContextManager[JdmTransaction]: ...


def required_inputs(content: Mapping[str, Any]) -> dict[str, UUID]:
    config = content.get("_config")
    if not isinstance(config, Mapping):
        raise JdmRuntimeError("JDM_MODEL_INVALID")
    raw_mappings = config.get("inputMappings", {})
    raw_sources = config.get("sourceEntityInstanceIds", ())
    if not isinstance(raw_mappings, Mapping) or not isinstance(
        raw_sources,
        Sequence,
    ) or isinstance(raw_sources, (str, bytes)):
        raise JdmRuntimeError("JDM_MODEL_INVALID")

    try:
        mappings = {
            str(field): UUID(str(entity_id))
            for field, entity_id in raw_mappings.items()
            if str(field).strip()
        }
        for value in raw_sources:
            entity_id = UUID(str(value))
            mappings.setdefault(str(entity_id), entity_id)
    except (TypeError, ValueError) as exc:
        raise JdmRuntimeError("JDM_MODEL_INVALID") from exc
    if not mappings:
        raise JdmRuntimeError("JDM_MODEL_INVALID")
    return mappings


def evaluate_model(model: JdmModel, event: FrameOutboxEvent) -> JdmExecution:
    digest = _model_digest(model.content)
    if model.configuration_revision > event.configuration_revision:
        return _execution(
            model,
            event,
            digest=digest,
            status="rejected",
            reason_code="JDM_MODEL_CONFIGURATION_MISMATCH",
        )

    try:
        mappings = required_inputs(model.content)
    except JdmRuntimeError as exc:
        return _execution(
            model,
            event,
            digest=digest,
            status="rejected",
            reason_code=exc.code,
        )

    changes = {item.entity_instance_id: item for item in event.l2_changes}
    inputs: dict[str, dict[str, Any]] = {}
    context: dict[str, Any] = {}
    for field, entity_id in mappings.items():
        change = changes.get(entity_id)
        if change is None or change.value.value is None:
            return _execution(
                model,
                event,
                digest=digest,
                status="rejected",
                reason_code="JDM_INPUT_MISSING",
                inputs=inputs,
            )
        evidence = _input_evidence(change)
        inputs[field] = evidence
        if change.quality is not TrunkQuality.GOOD:
            return _execution(
                model,
                event,
                digest=digest,
                status="rejected",
                reason_code="JDM_INPUT_QUALITY_NOT_GOOD",
                inputs=inputs,
            )
        if change.observed_at is None:
            return _execution(
                model,
                event,
                digest=digest,
                status="rejected",
                reason_code="JDM_INPUT_TIMESTAMP_MISSING",
                inputs=inputs,
            )
        context[field] = _json_value(change.value.value)

    try:
        result = evaluate_model_content(model.content, context)
        if result.get("error"):
            raise JdmRuntimeError("JDM_EVALUATION_FAILED")
        outputs = result["outputs"]
        actions = result["actions"]
    except Exception:
        return _execution(
            model,
            event,
            digest=digest,
            status="rejected",
            reason_code="JDM_EVALUATION_FAILED",
            inputs=inputs,
        )

    return _execution(
        model,
        event,
        digest=digest,
        status="executed",
        reason_code=None,
        inputs=inputs,
        outputs=outputs,
        actions=actions,
    )


def evaluate_model_content(
    content: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate editor input without persistence or runtime side effects."""
    result = evaluate_rule(dict(content), _json_mapping(context))
    return {
        "triggered": bool(result.get("triggered")),
        "actions": [
            _json_mapping(action)
            for action in result.get("actions", ())
            if isinstance(action, Mapping)
        ],
        "outputs": _json_mapping(result.get("outputs", {})),
        "error": None if result.get("error") is None else str(result["error"]),
        "engine": str(result.get("engine", "error")),
    }


class JdmRuntime:
    def __init__(self, repository: JdmRepository) -> None:
        self._repository = repository
        self._empty_configuration_revision: int | None = None

    def submit_frame(
        self,
        event: FrameOutboxEvent,
    ) -> tuple[JdmExecution, ...]:
        if self._empty_configuration_revision == event.configuration_revision:
            return ()
        with self._repository.transaction() as transaction:
            models = transaction.active_models(event.configuration_revision)
            if not models:
                self._empty_configuration_revision = event.configuration_revision
                return ()
            if not transaction.begin_committed_frame(
                "jdm",
                event.frame_id,
                event.frame_sequence,
                event.configuration_revision,
            ):
                return ()
            executions = tuple(
                evaluate_model(model, event)
                for model in models
            )
            for execution in executions:
                transaction.save_execution(execution)
            return executions


def _execution(
    model: JdmModel,
    event: FrameOutboxEvent,
    *,
    digest: str,
    status: str,
    reason_code: str | None,
    inputs: dict[str, dict[str, Any]] | None = None,
    outputs: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> JdmExecution:
    return JdmExecution(
        id=uuid5(
            NAMESPACE_URL,
            f"zizu/jdm/{model.id}/{model.version}/{event.frame_id}",
        ),
        rule_id=model.id,
        rule_version=model.version,
        frame_id=event.frame_id,
        frame_sequence=event.frame_sequence,
        configuration_revision=event.configuration_revision,
        model_digest=digest,
        status=status,
        reason_code=reason_code,
        inputs=inputs or {},
        outputs=outputs or {},
        actions=actions or [],
    )


def _input_evidence(change) -> dict[str, Any]:
    return {
        "entity_instance_id": str(change.entity_instance_id),
        "event_id": str(change.event_id),
        "value": _json_value(change.value.value),
        "data_type": change.value.kind.value,
        "unit": change.unit,
        "quality": int(change.quality),
        "observed_at": _optional_utc_iso(change.observed_at),
        "processing_revision_id": (
            None
            if change.processing_revision_id is None
            else str(change.processing_revision_id)
        ),
        "source_digest": change.source_digest,
    }


def _model_digest(content: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _json_value(content),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_mapping(value: Any) -> dict[str, Any]:
    normalized = _json_value(value)
    return normalized if isinstance(normalized, dict) else {"result": normalized}


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _optional_utc_iso(value: datetime | None) -> str | None:
    return None if value is None else _utc_iso(value)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("JDM input timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
