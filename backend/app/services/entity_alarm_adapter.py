"""实体实例观测到 ADR-0004 告警状态机的唯一适配器。"""
from __future__ import annotations

from uuid import UUID

from app.services.alarm_definition_dispatch import AlarmDefinitionDispatcher
from app.services.alarm_runtime import (
    AlarmDefinitionCatalog,
    AlarmObservation,
    AlarmOutcome,
    AlarmRuntime,
)
from app.services.entity_instance_registry import EntityInstanceError
from app.services.entity_instance_runtime import EntityInstanceRuntime


class EntityAlarmAdapter:
    """只投递已确认实体实例的观测，不接触物理点位。"""

    def __init__(
        self,
        definitions: AlarmDefinitionCatalog,
        entity_runtime: EntityInstanceRuntime,
        alarm_runtime: AlarmRuntime,
    ) -> None:
        self._definitions = definitions
        self._entity_runtime = entity_runtime
        self._alarm_runtime = alarm_runtime
        self._definition_dispatcher = AlarmDefinitionDispatcher(
            definitions,
            alarm_runtime,
        )

    def submit_entity(self, entity_instance_id: UUID) -> tuple[AlarmOutcome, ...]:
        observation = self._entity_runtime.read_for_alarm(entity_instance_id)
        effective_quality = (
            observation.quality
            if observation.fresh and observation.quality_good
            else 0
        )
        outcomes: list[AlarmOutcome] = []
        for definition in self._definition_dispatcher.for_entity(entity_instance_id):
            outcomes.append(
                self._alarm_runtime.submit(
                    AlarmObservation(
                        definition_id=definition.id,
                        entity_instance_id=entity_instance_id,
                        observed_at=observation.observed_at,
                        value=observation.value,
                        quality=effective_quality,
                        source_kind="entity_instance",
                        source_ref=str(entity_instance_id),
                        evidence={
                            "definition_id": observation.definition_id,
                            "node_id": str(observation.node_id),
                            "node_key": observation.node_key,
                            "data_type": observation.data_type,
                            "unit": observation.unit,
                            "fresh": observation.fresh,
                            "source_quality": observation.quality,
                            **observation.source_evidence(),
                        },
                        max_observation_gap_seconds=(
                            observation.max_observation_gap_seconds
                        ),
                    )
                )
            )
        return tuple(outcomes)

    def submit_all(
        self,
        *,
        exclude_entity_instance_ids: set[UUID] | None = None,
    ) -> tuple[AlarmOutcome, ...]:
        """Submit only instances not already represented by this flush's tag samples."""
        outcomes: list[AlarmOutcome] = []
        entity_ids = {
            definition.entity_instance_id
            for definition in self._definitions.all_definitions()
        }
        entity_ids.difference_update(exclude_entity_instance_ids or set())
        for entity_instance_id in entity_ids:
            try:
                outcomes.extend(self.submit_entity(entity_instance_id))
            except EntityInstanceError:
                continue
        return tuple(outcomes)


def build_postgres_entity_alarm_adapter() -> EntityAlarmAdapter:
    """Compose the production adapter without making the pipeline depend on HTTP."""
    from app.services.alarm_postgres import (
        PostgresAlarmDefinitionCatalog,
        PostgresAlarmRepository,
    )
    from app.services.entity_instance_postgres import (
        PostgresEntityInstanceRepository,
        PostgresObservationCatalog,
        PostgresSourceCatalog,
    )
    from app.services.entity_instance_registry import EntityInstanceRegistry
    from app.services.solution_delivery_repository import PostgresDeliveryRepository

    entity_repository = PostgresEntityInstanceRepository()
    registry = EntityInstanceRegistry(
        entity_repository,
        PostgresSourceCatalog(),
        PostgresDeliveryRepository().site_configuration_version,
    )
    entity_runtime = EntityInstanceRuntime(registry, PostgresObservationCatalog())
    definitions = PostgresAlarmDefinitionCatalog()
    return EntityAlarmAdapter(
        definitions,
        entity_runtime,
        AlarmRuntime(definitions, PostgresAlarmRepository()),
    )
