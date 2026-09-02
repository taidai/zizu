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
        return self.for_entities(frozenset({entity_instance_id})).get(
            entity_instance_id,
            (),
        )

    def for_entities(
        self,
        entity_instance_ids: frozenset[UUID],
    ) -> dict[UUID, tuple[AlarmDefinition, ...]]:
        if not entity_instance_ids:
            return {}
        current = tuple(
            definition
            for definition in self._definitions.all_definitions()
            if definition.entity_instance_id in entity_instance_ids
        )
        all_versions = {
            definition.id: definition
            for definition in self._definitions.all_versions()
        }
        historical: dict[UUID, dict[UUID, AlarmDefinition]] = {
            entity_id: {} for entity_id in entity_instance_ids
        }
        assets_with_open_events: dict[UUID, set[str]] = {
            entity_id: set() for entity_id in entity_instance_ids
        }
        for event in self._alarm_runtime.list_open_for_entities(entity_instance_ids):
            if event.state in OPEN_STATES:
                definition = all_versions.get(event.definition_id)
                if definition is not None:
                    historical[event.entity_instance_id][definition.id] = definition
                    assets_with_open_events[event.entity_instance_id].add(
                        definition.asset_id
                    )
        result: dict[UUID, tuple[AlarmDefinition, ...]] = {}
        for entity_id in entity_instance_ids:
            definitions = {
                definition.id: definition
                for definition in current
                if definition.entity_instance_id == entity_id
                and definition.asset_id not in assets_with_open_events[entity_id]
            }
            definitions.update(historical[entity_id])
            result[entity_id] = tuple(
                definitions[key] for key in sorted(definitions, key=str)
            )
        return result

    def for_asset(
        self,
        asset_id: str,
        entity_instance_id: UUID,
    ) -> tuple[AlarmDefinition, ...]:
        """Resolve one stable package alarm asset, including an open historical version."""
        return tuple(
            definition
            for definition in self.for_entity(entity_instance_id)
            if definition.asset_id == asset_id
        )

    def current_for_asset(
        self,
        asset_id: str,
        entity_instance_id: UUID,
    ) -> tuple[AlarmDefinition, ...]:
        """Resolve only a currently installed definition for configuration checks."""
        return tuple(
            definition
            for definition in self._definitions.for_entity(entity_instance_id)
            if definition.asset_id == asset_id
        )
