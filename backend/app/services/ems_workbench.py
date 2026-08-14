"""Resolve a package-configured EMS workbench without executing package UI code."""
from __future__ import annotations

from typing import Any

from app.services.entity_instance_catalog import EntityInstanceCatalog, EntityInstanceDescriptor
from app.services.entity_instance_registry import EntityInstanceError
from app.services.entity_instance_runtime import EntityInstanceRuntime
from app.services.solution_delivery_contracts import DeliveryError, DeliveryRepository


class EmsWorkbench:
    """One read interface hides package lookup, reference resolution and live observations."""

    def __init__(
        self,
        delivery: DeliveryRepository,
        catalog: EntityInstanceCatalog,
        runtime: EntityInstanceRuntime,
    ) -> None:
        self._delivery = delivery
        self._catalog = catalog
        self._runtime = runtime

    def read(self) -> dict[str, Any]:
        workbench, descriptors = self._installed_workbench()
        configuration_version = self._delivery.site_configuration_version()
        return {
            "workbench_id": workbench["id"],
            "site_configuration_version": configuration_version,
            "navigation": list(workbench["navigation"]),
            "groups": [
                {
                    "id": group["id"],
                    "label": group["label"],
                    "entities": self._live_entities(group["entities"], descriptors),
                }
                for group in workbench["groups"]
            ],
            "kpis": [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "entities": self._live_entities([item["entity"]], descriptors),
                }
                for item in workbench["kpis"]
            ],
            "trends": [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "default_range": item["default_range"],
                    "entities": self._entity_descriptors(item["entities"], descriptors),
                }
                for item in workbench["trends"]
            ],
            "alarms": dict(workbench["alarms"]),
            "controls": {
                **workbench["controls"],
                "entities": [
                    self._descriptor(item)
                    for item in descriptors
                    if item.direction in {"W", "RW"}
                ],
            },
        }

    def trend(self, trend_id: str, range_key: str) -> dict[str, Any]:
        """Read a declared chart only; callers cannot query arbitrary source tags."""
        workbench, descriptors = self._installed_workbench()
        trend = next((item for item in workbench["trends"] if item["id"] == trend_id), None)
        if trend is None:
            raise DeliveryError("WORKBENCH_TREND_NOT_FOUND", "EMS workbench trend is not configured")
        if range_key not in {"1h", "24h", "7d", "30d"}:
            raise DeliveryError("WORKBENCH_TREND_RANGE_INVALID", "EMS workbench trend range is invalid")
        return {
            "id": trend["id"],
            "label": trend["label"],
            "range": range_key,
            "series": [
                {
                    **self._descriptor(descriptor),
                    "points": [
                        {
                            "ts": observation.observed_at.isoformat(),
                            "value": observation.value,
                            "quality": observation.quality,
                        }
                        for observation in self._runtime.history(descriptor.id, range_key)
                    ],
                }
                for descriptor in self._resolve_references(trend["entities"], descriptors)
            ],
        }

    def _installed_workbench(self) -> tuple[dict[str, Any], tuple[EntityInstanceDescriptor, ...]]:
        configuration_version = self._delivery.site_configuration_version()
        if configuration_version < 1:
            raise DeliveryError("WORKBENCH_NOT_INSTALLED", "No site configuration is installed")
        configuration = self._delivery.get_site_configuration_version(configuration_version)
        if configuration is None:
            raise DeliveryError("WORKBENCH_NOT_INSTALLED", "Current site configuration is unavailable")
        installation = self._delivery.get_installation(configuration.installation_id)
        if installation is None:
            raise DeliveryError("WORKBENCH_NOT_INSTALLED", "Current installation is unavailable")
        package = self._delivery.package_for_installation(installation)
        if package is None:
            raise DeliveryError("WORKBENCH_NOT_INSTALLED", "Installed package is unavailable")
        workbenches = package.manifest.get("_workbench_assets", [])
        if len(workbenches) != 1:
            raise DeliveryError("WORKBENCH_NOT_CONFIGURED", "Installed package has no EMS workbench")

        workbench = workbenches[0]
        allowed_ids = set(installation.entity_instance_ids)
        descriptors = tuple(item for item in self._catalog.list() if item.id in allowed_ids)
        return workbench, descriptors

    def _live_entities(
        self,
        references: list[dict[str, str]],
        descriptors: tuple[EntityInstanceDescriptor, ...],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for descriptor in self._resolve_references(references, descriptors):
            public = self._descriptor(descriptor)
            try:
                observation = self._runtime.read(descriptor.id)
            except EntityInstanceError as exc:
                result.append({**public, "status": "unavailable", "code": exc.code})
            else:
                result.append(
                    {
                        **public,
                        "status": "available",
                        "value": observation.value,
                        "observed_at": observation.observed_at.isoformat(),
                        "quality": observation.quality,
                    }
                )
        return result

    def _entity_descriptors(
        self,
        references: list[dict[str, str]],
        descriptors: tuple[EntityInstanceDescriptor, ...],
    ) -> list[dict[str, Any]]:
        return [self._descriptor(item) for item in self._resolve_references(references, descriptors)]

    @staticmethod
    def _descriptor(item: EntityInstanceDescriptor) -> dict[str, Any]:
        return {
            "entity_instance_id": str(item.id),
            "slot_id": item.slot_id,
            "instance_key": item.instance_key,
            "definition_id": item.definition_id,
            "display_name": item.display_name,
            "data_type": item.data_type,
            "unit": item.unit,
            "direction": item.direction,
        }

    @staticmethod
    def _resolve_references(
        references: list[dict[str, str]],
        descriptors: tuple[EntityInstanceDescriptor, ...],
    ) -> list[EntityInstanceDescriptor]:
        result: list[EntityInstanceDescriptor] = []
        for reference in references:
            matched = [
                item
                for item in descriptors
                if item.slot_id == reference["slot"]
                and item.definition_id == reference["definition"]
            ]
            if not matched:
                raise DeliveryError(
                    "WORKBENCH_REFERENCE_UNRESOLVED",
                    "EMS workbench reference is not installed or no longer active",
                )
            result.extend(sorted(matched, key=lambda item: (item.instance_key, str(item.id))))
        return result
