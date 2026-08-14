"""Choose exactly the definition versions that may receive one observation."""
from __future__ import annotations

from uuid import UUID

from app.services.alarm_runtime import (
    AlarmDefinition,
    AlarmDefinitionCatalog,
    AlarmRuntime,
    OPEN_STATES,
)


class AlarmDefinitionDispatcher:
    """Hide current-versus-historical definition selection from source adapters."""

    def __init__(
        self,
        definitions: AlarmDefinitionCatalog,
        alarm_runtime: AlarmRuntime,
    ) -> None:
        self._definitions = definitions
        self._alarm_runtime = alarm_runtime

    def for_entity(self, entity_instance_id: UUID) -> tuple[AlarmDefinition, ...]:
        current = self._definitions.for_entity(entity_instance_id)
        historical: dict[UUID, AlarmDefinition] = {}
        assets_with_open_events: set[str] = set()
        for event in self._alarm_runtime.list():
            if (
                event.entity_instance_id == entity_instance_id
                and event.state in OPEN_STATES
            ):
                definition = self._definitions.get(event.definition_id)
                if definition is not None:
                    historical[definition.id] = definition
                    assets_with_open_events.add(definition.asset_id)
        definitions = {
            definition.id: definition
            for definition in current
            if definition.asset_id not in assets_with_open_events
        }
        definitions.update(historical)
        return tuple(definitions[key] for key in sorted(definitions, key=str))
