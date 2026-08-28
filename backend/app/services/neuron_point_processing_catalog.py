"""Read-only normalization of a Neuron point catalog for point-processing plans."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any, Protocol


_TYPE_BY_CODE = {
    3: ("INT16", "INT"),
    4: ("UINT16", "INT"),
    5: ("INT32", "INT"),
    6: ("UINT32", "INT"),
    7: ("INT64", "INT"),
    8: ("UINT64", "INT"),
    9: ("FLOAT", "FLOAT"),
    10: ("DOUBLE", "FLOAT"),
    11: ("BIT", "BOOL"),
    13: ("STRING", "STRING"),
}


class NeuronPointReader(Protocol):
    def get_groups(self, node_name: str) -> list[dict]: ...

    def get_tags(self, node_name: str, group_name: str) -> list[dict]: ...


@dataclass(frozen=True)
class ScannedPoint:
    group: str
    group_interval_ms: int
    name: str
    address: str
    wire_data_type: str
    value_data_type: str
    decimal: float | None
    read_only: bool


@dataclass(frozen=True)
class ScannedPointCatalog:
    node_name: str
    group_interval_ms: int
    digest: str
    points: tuple[ScannedPoint, ...]
    blockers: tuple[Mapping[str, str], ...]


class NeuronPointCatalog:
    """Only calls Neuron GET surfaces and returns deterministic evidence."""

    def __init__(self, client: NeuronPointReader) -> None:
        self._client = client

    def scan(self, node_name: str) -> ScannedPointCatalog:
        return self._scan(node_name, selected_groups=None)

    def scan_selected(
        self,
        node_name: str,
        selected_groups: tuple[str, ...],
    ) -> ScannedPointCatalog:
        normalized = {group.strip() for group in selected_groups if group.strip()}
        return self._scan(node_name, selected_groups=normalized)

    def _scan(
        self,
        node_name: str,
        *,
        selected_groups: set[str] | None,
    ) -> ScannedPointCatalog:
        all_groups = self._client.get_groups(node_name)
        groups = [
            group
            for group in all_groups
            if selected_groups is None or str(group.get("name", "")).strip() in selected_groups
        ]
        blockers: list[Mapping[str, str]] = []
        intervals = {
            int(group.get("interval", 0))
            for group in groups
            if isinstance(group, Mapping)
            and isinstance(group.get("interval"), (int, float))
            and not isinstance(group.get("interval"), bool)
            and int(group["interval"]) > 0
        }
        if not groups:
            blockers.append(_blocker("NEURON_CATALOG_EMPTY", "group"))
        group_interval_ms = next(iter(intervals)) if len(intervals) == 1 else 0

        points: list[ScannedPoint] = []
        for group in sorted(groups, key=lambda item: str(item.get("name", ""))):
            group_name = str(group.get("name", "")).strip()
            if not group_name:
                continue
            interval = group.get("interval")
            normalized_interval = (
                int(interval)
                if isinstance(interval, (int, float))
                and not isinstance(interval, bool)
                and int(interval) > 0
                else 0
            )
            for raw in self._client.get_tags(node_name, group_name):
                point, _point_blockers = _normalize_point(
                    group_name,
                    normalized_interval,
                    raw,
                )
                if point is not None:
                    points.append(point)

        canonical_points = tuple(
            sorted(points, key=lambda item: (item.address, item.group, item.name))
        )
        canonical_blockers = tuple(
            sorted(blockers, key=lambda item: (item["code"], item["resource_key"]))
        )
        digest = hashlib.sha256(
            json.dumps(
                {
                    "node_name": node_name,
                    "group_interval_ms": group_interval_ms,
                    "points": [asdict(item) for item in canonical_points],
                    "blockers": [dict(item) for item in canonical_blockers],
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return ScannedPointCatalog(
            node_name=node_name,
            group_interval_ms=group_interval_ms,
            digest=digest,
            points=canonical_points,
            blockers=canonical_blockers,
        )


def _normalize_point(
    group_name: str,
    group_interval_ms: int,
    raw: Mapping[str, Any],
) -> tuple[ScannedPoint | None, tuple[Mapping[str, str], ...]]:
    name = str(raw.get("name", "")).strip()
    address = str(raw.get("address", "")).strip()
    type_contract = _TYPE_BY_CODE.get(raw.get("type"))
    if not name or not address or type_contract is None:
        resource = address or name or group_name
        return None, (_blocker("NEURON_POINT_CONTRACT_INVALID", resource),)
    attribute = raw.get("attribute", 0)
    read_only = (
        attribute == "Read"
        or (
            isinstance(attribute, int)
            and not isinstance(attribute, bool)
            and bool(attribute & 0x01)
            and not bool(attribute & 0x02)
        )
    )
    blockers = (
        () if read_only else (_blocker("NEURON_POINT_NOT_READ_ONLY", address),)
    )
    decimal = raw.get("decimal")
    normalized_decimal = (
        float(decimal)
        if isinstance(decimal, (int, float)) and not isinstance(decimal, bool)
        else None
    )
    value_data_type = (
        "FLOAT"
        if normalized_decimal not in {None, 0.0} and type_contract[1] == "INT"
        else type_contract[1]
    )
    return (
        ScannedPoint(
            group=group_name,
            group_interval_ms=group_interval_ms,
            name=name,
            address=address,
            wire_data_type=type_contract[0],
            value_data_type=value_data_type,
            decimal=normalized_decimal,
            read_only=read_only,
        ),
        blockers,
    )


def _normalized_name(value: str) -> str:
    return "".join(value.split()).casefold()


def _blocker(code: str, resource_key: str) -> Mapping[str, str]:
    return MappingProxyType({"code": code, "resource_key": resource_key})
