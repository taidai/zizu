"""Deterministic preview model for importing Neuron groups into node-owned L0."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import inspect
import json
from typing import Any, Awaitable, Callable, Literal, Protocol
from uuid import UUID, uuid4

from app.services.neuron_point_processing_catalog import ScannedPoint


ImportAction = Literal["create", "update", "unchanged", "conflict"]


class NeuronTagImportError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(f"{code}: {message or code}")
        self.code = code


@dataclass(frozen=True)
class ExistingNeuronTag:
    id: UUID
    node_id: UUID
    name: str
    display_name: str | None
    data_type: str
    wire_data_type: str | None
    source_path: str
    source_address: str | None
    decimal: float | None
    read_write: str
    enabled: bool
    l1_bound: bool


@dataclass(frozen=True)
class NeuronTagImportItem:
    source_path: str
    group: str
    name: str
    source_address: str
    wire_data_type: str
    value_data_type: str
    decimal: float | None
    read_write: str
    action: ImportAction
    reason: str | None = None
    after_id: UUID | None = None


@dataclass(frozen=True)
class NeuronTagImportPreview:
    node_id: UUID
    neuron_node: str
    selected_groups: tuple[str, ...]
    base_configuration_revision: int
    digest: str
    items: tuple[NeuronTagImportItem, ...]

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(item.action for item in self.items))

    @property
    def has_conflicts(self) -> bool:
        return any(item.action == "conflict" for item in self.items)


def plan_neuron_tag_import(
    *,
    node_id: UUID,
    neuron_node: str,
    selected_groups: tuple[str, ...],
    points: tuple[ScannedPoint, ...],
    existing: tuple[ExistingNeuronTag, ...],
    base_configuration_revision: int,
) -> NeuronTagImportPreview:
    normalized_node = neuron_node.strip()
    groups = tuple(sorted({group.strip() for group in selected_groups if group.strip()}))
    existing_by_source = {
        tag.source_path: tag
        for tag in existing
        if tag.node_id == node_id and tag.source_path
    }
    items: list[NeuronTagImportItem] = []
    for point in sorted(points, key=lambda item: (item.group, item.name, item.address)):
        if point.group not in groups:
            continue
        source_path = f"{normalized_node}/{point.group}/{point.name}"
        current = existing_by_source.get(source_path)
        read_write = "R" if point.read_only else "RW"
        action: ImportAction
        reason: str | None = None
        after_id: UUID | None = None
        if current is None:
            action = "create"
        else:
            contract_changed = any(
                (
                    current.data_type != point.value_data_type,
                    current.wire_data_type != point.wire_data_type,
                    current.source_address != point.address,
                    current.decimal != point.decimal,
                )
            )
            desired_changed = any(
                (
                    current.name != point.name,
                    current.display_name != point.name,
                    contract_changed,
                    current.read_write != read_write,
                    not current.enabled,
                )
            )
            if current.l1_bound and contract_changed:
                action = "conflict"
                reason = "ACTIVE_L1_CONTRACT_CONFLICT"
            elif desired_changed:
                action = "update"
                after_id = current.id
            else:
                action = "unchanged"
        items.append(
            NeuronTagImportItem(
                source_path=source_path,
                group=point.group,
                name=point.name,
                source_address=point.address,
                wire_data_type=point.wire_data_type,
                value_data_type=point.value_data_type,
                decimal=point.decimal,
                read_write=read_write,
                action=action,
                reason=reason,
                after_id=after_id,
            )
        )
    canonical = {
        "node_id": str(node_id),
        "neuron_node": normalized_node,
        "selected_groups": groups,
        "base_configuration_revision": base_configuration_revision,
        "items": [
            {
                **asdict(item),
                "after_id": None if item.after_id is None else str(item.after_id),
            }
            for item in items
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return NeuronTagImportPreview(
        node_id=node_id,
        neuron_node=normalized_node,
        selected_groups=groups,
        base_configuration_revision=base_configuration_revision,
        digest=digest,
        items=tuple(items),
    )


class NeuronTagImportRepository(Protocol):
    def apply(
        self,
        preview: NeuronTagImportPreview,
        *,
        actor: str,
    ) -> dict[str, Any]: ...


async def apply_neuron_tag_import(
    preview: NeuronTagImportPreview,
    *,
    preview_digest: str,
    actor: str,
    repository: NeuronTagImportRepository,
    runtime_gate: Any,
    reload_runtime: Callable[[], Awaitable[None] | None],
) -> dict[str, Any]:
    """Atomically publish one preview, then reload before opening capture."""
    import asyncio

    if preview_digest != preview.digest:
        raise NeuronTagImportError("NEURON_IMPORT_PREVIEW_STALE")
    if preview.has_conflicts:
        raise NeuronTagImportError("NEURON_IMPORT_CONFLICT")
    changed = sum(
        count for action, count in preview.counts.items() if action in {"create", "update"}
    )
    if changed == 0:
        return {
            "configuration_revision": preview.base_configuration_revision,
            "counts": preview.counts,
            "status": "unchanged",
        }
    await asyncio.to_thread(
        runtime_gate.begin_configuration_publish,
        preview.base_configuration_revision,
    )
    try:
        result = await asyncio.to_thread(repository.apply, preview, actor=actor)
    except Exception:
        runtime_gate.cancel_configuration_publish()
        raise
    reload_result = reload_runtime()
    if inspect.isawaitable(reload_result):
        await reload_result
    await asyncio.to_thread(runtime_gate.reconcile_configuration_runtime)
    return result


class PostgresNeuronTagImports:
    def __init__(self, connection_factory: Callable[[], Any] | None = None) -> None:
        if connection_factory is None:
            from app.services.telemetry_store import get_connection

            connection_factory = get_connection
        self._connection_factory = connection_factory

    def preview(
        self,
        *,
        node_id: UUID,
        neuron_node: str,
        selected_groups: tuple[str, ...],
        points: tuple[ScannedPoint, ...],
    ) -> NeuronTagImportPreview:
        with self._connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT enabled FROM t_nodes WHERE id=%s",
                    (str(node_id),),
                )
                node = cursor.fetchone()
                if node is None or not bool(node[0]):
                    raise NeuronTagImportError("NEURON_IMPORT_NODE_NOT_FOUND")
                cursor.execute(
                    "SELECT current_revision FROM t_configuration_state WHERE singleton=TRUE"
                )
                revision = cursor.fetchone()
                if revision is None:
                    raise NeuronTagImportError("CONFIGURATION_REVISION_UNAVAILABLE")
                cursor.execute(
                    """
                    SELECT tag.id,tag.node_id,tag.name,tag.display_name,tag.data_type,
                           tag.wire_data_type,tag.source_path,tag.source_address,
                           tag.decimal,tag.read_write,tag.enabled,
                           EXISTS (
                             SELECT 1
                             FROM t_point_processing_input_bindings AS binding
                             JOIN t_installed_point_processings AS installed
                               ON installed.id=binding.installed_processing_id
                             WHERE binding.l0_tag_id=tag.id
                               AND installed.current=TRUE
                           ) AS l1_bound
                    FROM t_tags AS tag
                    WHERE tag.node_id=%s
                      AND lower(COALESCE(tag.source_type,''))='neuron'
                      AND tag.source_path IS NOT NULL
                    """,
                    (str(node_id),),
                )
                existing = tuple(
                    ExistingNeuronTag(
                        id=UUID(str(row[0])),
                        node_id=UUID(str(row[1])),
                        name=row[2],
                        display_name=row[3],
                        data_type=row[4],
                        wire_data_type=row[5],
                        source_path=row[6],
                        source_address=row[7],
                        decimal=None if row[8] is None else float(row[8]),
                        read_write=row[9],
                        enabled=bool(row[10]),
                        l1_bound=bool(row[11]),
                    )
                    for row in cursor.fetchall()
                )
        return plan_neuron_tag_import(
            node_id=node_id,
            neuron_node=neuron_node,
            selected_groups=selected_groups,
            points=points,
            existing=existing,
            base_configuration_revision=int(revision[0]),
        )

    def apply(
        self,
        preview: NeuronTagImportPreview,
        *,
        actor: str,
    ) -> dict[str, Any]:
        from app.services.configuration_revision_postgres import (
            PostgresConfigurationRevisions,
        )

        if preview.has_conflicts:
            raise NeuronTagImportError("NEURON_IMPORT_CONFLICT")
        revisions = PostgresConfigurationRevisions()
        with self._connection_factory() as connection:
            try:
                with connection.cursor() as cursor:
                    for item in preview.items:
                        if item.action == "create":
                            cursor.execute(
                                """
                                INSERT INTO t_tags
                                  (id,node_id,tag_type,name,display_name,data_type,
                                   source_type,source_path,source_address,
                                   wire_data_type,value_data_type,decimal,
                                   read_write,read_only,enabled)
                                VALUES (%s,%s,'PHYSICAL',%s,%s,%s,'neuron',%s,%s,%s,%s,
                                        %s,%s,%s,TRUE)
                                """,
                                (
                                    str(uuid4()),
                                    str(preview.node_id),
                                    item.name,
                                    item.name,
                                    item.value_data_type,
                                    item.source_path,
                                    item.source_address,
                                    item.wire_data_type,
                                    item.value_data_type,
                                    item.decimal,
                                    item.read_write,
                                    item.read_write == "R",
                                ),
                            )
                        elif item.action == "update" and item.after_id is not None:
                            cursor.execute(
                                """
                                UPDATE t_tags
                                SET name=%s,display_name=%s,data_type=%s,
                                    source_address=%s,wire_data_type=%s,
                                    value_data_type=%s,decimal=%s,read_write=%s,
                                    read_only=%s,enabled=TRUE
                                WHERE id=%s AND node_id=%s
                                  AND lower(COALESCE(source_type,''))='neuron'
                                  AND source_path=%s
                                """,
                                (
                                    item.name,
                                    item.name,
                                    item.value_data_type,
                                    item.source_address,
                                    item.wire_data_type,
                                    item.value_data_type,
                                    item.decimal,
                                    item.read_write,
                                    item.read_write == "R",
                                    str(item.after_id),
                                    str(preview.node_id),
                                    item.source_path,
                                ),
                            )
                            if cursor.rowcount != 1:
                                raise NeuronTagImportError("NEURON_IMPORT_PREVIEW_STALE")
                configuration_revision = revisions.publish(
                    transaction=connection,
                    base_revision=preview.base_configuration_revision,
                    actor=actor,
                    action="neuron_tag_import.apply",
                    resource_kind="node",
                    resource_id=str(preview.node_id),
                    before_digest=None,
                    after_digest=preview.digest,
                    details={
                        "neuron_node": preview.neuron_node,
                        "groups": list(preview.selected_groups),
                        "counts": preview.counts,
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "configuration_revision": configuration_revision,
            "counts": preview.counts,
            "status": "applied",
        }


__all__ = [
    "ExistingNeuronTag",
    "NeuronTagImportError",
    "NeuronTagImportItem",
    "NeuronTagImportPreview",
    "PostgresNeuronTagImports",
    "apply_neuron_tag_import",
    "plan_neuron_tag_import",
]
