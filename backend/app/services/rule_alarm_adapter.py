"""Rule observations adapted to the ADR-0004 unified alarm lifecycle."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

from app.services.alarm_definition_dispatch import AlarmDefinitionDispatcher
from app.services.alarm_runtime import (
    AlarmDefinitionCatalog,
    AlarmObservation,
    AlarmOutcome,
    AlarmRuntime,
)


@dataclass(frozen=True)
class RuleAlarmObservation:
    rule_id: UUID
    rule_version: int
    action_id: str
    alarm_definition: str
    entity_instance_id: UUID
    observed_at: datetime
    value: Any
    quality: int
    evidence: Mapping[str, Any]
    max_observation_gap_seconds: float | None = None


class RuleAlarmAdapter:
    """Adapt a configured rule result without exposing lifecycle decisions to rules."""

    def __init__(
        self,
        definitions: AlarmDefinitionCatalog,
        alarm_runtime: AlarmRuntime,
    ) -> None:
        self._runtime = alarm_runtime
        self._definitions = AlarmDefinitionDispatcher(definitions, alarm_runtime)

    def submit(self, observation: RuleAlarmObservation) -> tuple[AlarmOutcome, ...]:
        outcomes: list[AlarmOutcome] = []
        for definition in self._definitions.for_asset(
            observation.alarm_definition,
            observation.entity_instance_id,
        ):
            outcomes.append(
                self._runtime.submit(
                    AlarmObservation(
                        definition_id=definition.id,
                        entity_instance_id=observation.entity_instance_id,
                        observed_at=observation.observed_at,
                        value=observation.value,
                        quality=observation.quality,
                        source_kind="rule",
                        source_ref=f"{observation.rule_id}#{observation.action_id}",
                        max_observation_gap_seconds=observation.max_observation_gap_seconds,
                        evidence={
                            "rule_id": str(observation.rule_id),
                            "rule_version": str(observation.rule_version),
                            "action_id": observation.action_id,
                            "alarm_definition": observation.alarm_definition,
                            **dict(observation.evidence),
                        },
                    )
                )
            )
        return tuple(outcomes)

    def has_installed_definition(
        self,
        asset_id: str,
        entity_instance_id: UUID,
    ) -> bool:
        """Return whether this asset is currently installed for the target instance."""
        return bool(self._definitions.current_for_asset(asset_id, entity_instance_id))


def build_postgres_rule_alarm_adapter() -> RuleAlarmAdapter:
    """Compose the production rule source without giving it direct table access."""
    from app.services.alarm_postgres import (
        PostgresAlarmDefinitionCatalog,
        PostgresAlarmRepository,
    )

    definitions = PostgresAlarmDefinitionCatalog()
    return RuleAlarmAdapter(definitions, AlarmRuntime(definitions, PostgresAlarmRepository()))
