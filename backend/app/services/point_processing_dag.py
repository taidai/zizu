"""Validate the immutable site-wide dependency DAG for L2 processing."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import heapq
import json
from uuid import UUID


class PointProcessingDagError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DagSummary:
    order: tuple[UUID, ...]
    max_depth: int
    edge_count: int
    digest: str


def validate_processing_dag(
    *,
    existing_edges: tuple[tuple[UUID, UUID], ...],
    planned_edges: tuple[tuple[UUID, UUID], ...],
    max_depth: int = 8,
) -> DagSummary:
    if max_depth < 1:
        raise ValueError("maximum DAG depth must be positive")
    edges = tuple(
        sorted(set(existing_edges) | set(planned_edges), key=lambda item: (str(item[0]), str(item[1])))
    )
    if any(source == target for source, target in edges):
        raise PointProcessingDagError(
            "POINT_PROCESSING_DAG_CYCLE",
            "Point processing output cannot depend on itself",
        )
    nodes = {node for edge in edges for node in edge}
    outgoing: dict[UUID, set[UUID]] = {node: set() for node in nodes}
    incoming_count: dict[UUID, int] = {node: 0 for node in nodes}
    for source, target in edges:
        outgoing[source].add(target)
        incoming_count[target] += 1

    ready = [(str(node), node) for node in nodes if incoming_count[node] == 0]
    heapq.heapify(ready)
    order: list[UUID] = []
    depth = {node: 1 for node in nodes}
    while ready:
        _, current = heapq.heappop(ready)
        order.append(current)
        for target in sorted(outgoing[current], key=str):
            depth[target] = max(depth[target], depth[current] + 1)
            incoming_count[target] -= 1
            if incoming_count[target] == 0:
                heapq.heappush(ready, (str(target), target))

    if len(order) != len(nodes):
        raise PointProcessingDagError(
            "POINT_PROCESSING_DAG_CYCLE",
            "Point processing dependency graph contains a cycle",
        )
    actual_depth = max(depth.values(), default=0)
    if actual_depth > max_depth:
        raise PointProcessingDagError(
            "POINT_PROCESSING_DAG_DEPTH_EXCEEDED",
            "Point processing dependency graph exceeds the eight-layer limit",
        )
    material = json.dumps(
        [[str(source), str(target)] for source, target in edges],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return DagSummary(
        order=tuple(order),
        max_depth=actual_depth,
        edge_count=len(edges),
        digest=hashlib.sha256(material.encode("ascii")).hexdigest(),
    )
