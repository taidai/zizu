"""Validate the deliberately fixed EMS workbench asset grammar.

The package supplies data only.  Layout, charts and control execution stay in
the platform so an implementation engineer never ships browser code with a
station configuration.
"""
from __future__ import annotations

from typing import Any, Callable

from app.services.solution_delivery_contracts import DeliveryError


def validate_ems_workbench_assets(
    manifest: dict[str, Any],
    assets: dict[str, bytes],
    slots: tuple[dict[str, Any], ...],
    load_mapping: Callable[[bytes | None, str], dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Return normalized workbench data after rejecting arbitrary UI/config references."""
    declarations = [
        item for item in manifest["assets"] if item["kind"] == "ems_workbench"
    ]
    if len(declarations) > 1:
        raise DeliveryError("ASSET_REFERENCE_INVALID", "A package may declare one EMS workbench")
    slot_definitions = {
        slot["id"]: {definition["id"] for definition in slot["definitions"]}
        for slot in slots
    }
    normalized: list[dict[str, Any]] = []
    for declaration in declarations:
        raw = load_mapping(assets.get(declaration["path"]), "ASSET_REFERENCE_INVALID")
        expected_fields = {
            "schemaVersion", "id", "kind", "navigation", "groups", "kpis", "trends", "alarms", "controls",
        }
        if (
            set(raw) != expected_fields
            or raw.get("schemaVersion") != "zizu.ems-workbench/v1alpha1"
            or raw.get("id") != declaration["id"]
            or raw.get("kind") != "ems_workbench"
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench asset is invalid")
        navigation = _navigation(raw.get("navigation"))
        groups = _groups(raw.get("groups"), slot_definitions)
        kpis = _kpis(raw.get("kpis"), slot_definitions)
        trends = _trends(raw.get("trends"), slot_definitions)
        if not _entry_visibility(raw.get("alarms")) or not _entry_visibility(raw.get("controls")):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench entry visibility is invalid")
        normalized.append(
            {
                "id": declaration["id"],
                "navigation": navigation,
                "groups": groups,
                "kpis": kpis,
                "trends": trends,
                "alarms": {"visible": raw["alarms"]["visible"]},
                "controls": {"visible": raw["controls"]["visible"]},
            }
        )
    return tuple(normalized)


def _navigation(raw: Any) -> list[dict[str, str]]:
    builtin = {"overview", "trends", "alarms", "controls"}
    if not isinstance(raw, list) or not raw:
        raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench navigation is invalid")
    items: list[dict[str, str]] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "label"}
            or item.get("id") not in builtin
            or not isinstance(item.get("label"), str)
            or not item["label"].strip()
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench navigation is invalid")
        items.append({"id": item["id"], "label": item["label"].strip()})
    if len({item["id"] for item in items}) != len(items):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench navigation IDs must be unique")
    return items


def _groups(raw: Any, slot_definitions: dict[str, set[str]]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench groups are invalid")
    groups: list[dict[str, Any]] = []
    for group in raw:
        if (
            not isinstance(group, dict)
            or set(group) != {"id", "label", "entities"}
            or not isinstance(group.get("id"), str)
            or not group["id"].strip()
            or not isinstance(group.get("label"), str)
            or not group["label"].strip()
            or not isinstance(group.get("entities"), list)
            or not group["entities"]
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench group is invalid")
        groups.append(
            {
                "id": group["id"].strip(),
                "label": group["label"].strip(),
                "entities": _entity_refs(group["entities"], slot_definitions),
            }
        )
    if len({group["id"] for group in groups}) != len(groups):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench group IDs must be unique")
    return groups


def _kpis(raw: Any, slot_definitions: dict[str, set[str]]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench KPIs are invalid")
    items: list[dict[str, Any]] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "label", "entity"}
            or not isinstance(item.get("id"), str)
            or not item["id"].strip()
            or not isinstance(item.get("label"), str)
            or not item["label"].strip()
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench KPI is invalid")
        items.append(
            {
                "id": item["id"].strip(),
                "label": item["label"].strip(),
                "entity": _entity_refs([item.get("entity")], slot_definitions)[0],
            }
        )
    if len({item["id"] for item in items}) != len(items):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench KPI IDs must be unique")
    return items


def _trends(raw: Any, slot_definitions: dict[str, set[str]]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench trends are invalid")
    items: list[dict[str, Any]] = []
    for item in raw:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "label", "entities", "defaultRange"}
            or not isinstance(item.get("id"), str)
            or not item["id"].strip()
            or not isinstance(item.get("label"), str)
            or not item["label"].strip()
            or item.get("defaultRange") not in {"1h", "24h", "7d", "30d"}
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench trend is invalid")
        entities = _entity_refs(item.get("entities"), slot_definitions)
        if not entities:
            raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench trend needs an entity")
        items.append(
            {
                "id": item["id"].strip(),
                "label": item["label"].strip(),
                "entities": entities,
                "default_range": item["defaultRange"],
            }
        )
    if len({item["id"] for item in items}) != len(items):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench trend IDs must be unique")
    return items


def _entity_refs(raw: Any, slot_definitions: dict[str, set[str]]) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench entity references are invalid")
    references: list[dict[str, str]] = []
    for reference in raw:
        if (
            not isinstance(reference, dict)
            or set(reference) != {"slot", "definition"}
            or not isinstance(reference.get("slot"), str)
            or not isinstance(reference.get("definition"), str)
            or reference["definition"] not in slot_definitions.get(reference["slot"], set())
        ):
            raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench references an unknown entity")
        references.append({"slot": reference["slot"], "definition": reference["definition"]})
    if len({(item["slot"], item["definition"]) for item in references}) != len(references):
        raise DeliveryError("ASSET_REFERENCE_INVALID", "EMS workbench entity references must be unique")
    return references


def _entry_visibility(raw: Any) -> bool:
    return isinstance(raw, dict) and set(raw) == {"visible"} and isinstance(raw.get("visible"), bool)
