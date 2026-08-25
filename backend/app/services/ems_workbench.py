"""Fixed light-storage-charging EMS workbench over active L2 entities."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.entity_instance_catalog import EntityInstanceCatalog, EntityInstanceDescriptor
from app.services.entity_instance_registry import EntityInstanceError
from app.services.entity_instance_runtime import EntityInstanceRuntime


class EmsWorkbenchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_GROUPS = (
    ("pv", "光伏", ("pv.", "inverter.")),
    ("storage", "储能", ("storage.", "ess.", "pcs.", "bms.")),
    ("charging", "充电", ("charger.", "evse.")),
    ("grid", "电网与负荷", ("grid.", "meter.", "load.", "site.")),
)
_KPI_DEFINITIONS = (
    ("site-power", "站点功率", ("site.activePower", "grid.activePower")),
    ("pv-power", "光伏功率", ("pv.activePower", "inverter.activePower")),
    ("storage-power", "储能功率", ("storage.activePower", "pcs.activePower")),
    ("storage-soc", "储能 SOC", ("storage.soc", "bms.soc")),
    ("charging-power", "充电功率", ("charger.activePower", "evse.activePower")),
)


class EmsWorkbench:
    def __init__(self, catalog: EntityInstanceCatalog, runtime: EntityInstanceRuntime, configuration_revision: Callable[[], int]) -> None:
        self._catalog = catalog
        self._runtime = runtime
        self._configuration_revision = configuration_revision

    def read(self) -> dict[str, Any]:
        descriptors = self._catalog.list()
        groups = []
        for group_id, label, prefixes in _GROUPS:
            matched = tuple(item for item in descriptors if item.definition_id.lower().startswith(prefixes))
            if matched:
                groups.append({"id": group_id, "label": label, "entities": self._live_entities(matched)})
        assigned = {item["entity_instance_id"] for group in groups for item in group["entities"]}
        remaining = tuple(item for item in descriptors if str(item.id) not in assigned)
        if remaining:
            groups.append({"id": "other", "label": "其他设备", "entities": self._live_entities(remaining)})

        kpis = []
        for key, label, definitions in _KPI_DEFINITIONS:
            matched = next((item for definition in definitions for item in descriptors if item.definition_id == definition), None)
            if matched:
                kpis.append({"id": key, "label": label, "entities": self._live_entities((matched,))})
        numeric = tuple(item for item in descriptors if item.data_type.upper() in {"FLOAT", "INT"})
        trends = [{"id": "energy-flow", "label": "功率与能量趋势", "default_range": "24h", "entities": [self._descriptor(item) for item in numeric[:8]]}]
        controls = [self._descriptor(item) for item in descriptors if item.direction in {"W", "RW"}]
        return {
            "workbench_id": "fixed-light-storage-charging",
            "configuration_revision": self._configuration_revision(),
            "navigation": [
                {"id": "overview", "label": "总览"}, {"id": "trends", "label": "趋势"},
                {"id": "alarms", "label": "告警"}, {"id": "controls", "label": "控制"},
            ],
            "groups": groups, "kpis": kpis, "trends": trends,
            "alarms": {"visible": True},
            "controls": {"visible": bool(controls), "entities": controls},
        }

    def trend(self, trend_id: str, range_key: str) -> dict[str, Any]:
        if trend_id != "energy-flow":
            raise EmsWorkbenchError("WORKBENCH_TREND_NOT_FOUND", "EMS trend is not configured")
        if range_key not in {"1h", "24h", "7d", "30d"}:
            raise EmsWorkbenchError("WORKBENCH_TREND_RANGE_INVALID", "EMS trend range is invalid")
        descriptors = tuple(item for item in self._catalog.list() if item.data_type.upper() in {"FLOAT", "INT"})[:8]
        return {"id": trend_id, "label": "功率与能量趋势", "range": range_key, "series": [
            {**self._descriptor(item), "points": [{"ts": observation.observed_at.isoformat(), "value": observation.value, "quality": observation.quality} for observation in self._runtime.history(item.id, range_key)]}
            for item in descriptors
        ]}

    def _live_entities(self, descriptors: tuple[EntityInstanceDescriptor, ...]) -> list[dict[str, Any]]:
        result = []
        for item in descriptors:
            public = self._descriptor(item)
            try:
                observation = self._runtime.read(item.id)
            except EntityInstanceError as error:
                result.append({**public, "status": "unavailable", "code": error.code})
            else:
                result.append({**public, "status": "available", "value": observation.value, "observed_at": observation.observed_at.isoformat(), "quality": observation.quality})
        return result

    @staticmethod
    def _descriptor(item: EntityInstanceDescriptor) -> dict[str, Any]:
        return {
            "entity_instance_id": str(item.id), "node_id": str(item.node_id),
            "node_name": item.node_display_name, "definition_id": item.definition_id,
            "display_name": item.display_name, "data_type": item.data_type.lower(),
            "unit": item.unit, "direction": item.direction,
        }
