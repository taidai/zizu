"""Strict adapter for the single supported GoRules JDM execution semantics."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping

from loguru import logger

try:
    import zen as _zen  # type: ignore[import-not-found]  # noqa: N812

    logger.info(
        "[GoRules] zen-engine {} loaded",
        getattr(_zen, "__version__", "unknown"),
    )
except Exception as error:  # pragma: no cover - deployment dependency boundary
    _zen = None  # type: ignore[assignment]
    logger.warning("[GoRules] zen-engine unavailable: {}", error)


ALLOWED_NODE_TYPES = frozenset(
    {"inputNode", "decisionTableNode", "expressionNode", "outputNode"}
)


class StandardJdmError(ValueError):
    """Stable machine-readable failure from the only JDM adapter."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def compile_standard_jdm(content: Mapping[str, object]) -> str:
    """Validate and compile one standard JDM graph, returning its digest."""
    prepared = _prepare(content)
    _create_decision(prepared)
    return hashlib.sha256(_canonical_json(prepared).encode("utf-8")).hexdigest()


def evaluate_standard_jdm(
    content: Mapping[str, object],
    inputs: Mapping[str, object],
) -> dict[str, object]:
    """Evaluate one standard JDM graph without any alternate interpreter."""
    prepared = _prepare(content)
    decision = _create_decision(prepared)
    try:
        result = decision.evaluate(dict(inputs))
    except Exception as error:
        raise StandardJdmError("JDM_EVALUATION_FAILED", str(error)) from error
    if not isinstance(result, dict):
        raise StandardJdmError(
            "JDM_RESULT_INVALID",
            "GoRules must return an object",
        )
    return result


def evaluate_rule(jdm_content: dict, context: dict[str, Any]) -> dict[str, object]:
    """Temporary response-shape bridge; still executes strict standard JDM only."""
    try:
        outputs = evaluate_standard_jdm(jdm_content, context)
    except StandardJdmError as error:
        return {
            "triggered": False,
            "actions": [],
            "outputs": {},
            "error": str(error),
            "engine": "error",
        }
    result = outputs.get("result", outputs)
    return {
        "triggered": bool(result),
        "actions": [],
        "outputs": outputs,
        "error": None,
        "engine": "zen",
    }


def _prepare(content: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(content, Mapping):
        raise StandardJdmError("STANDARD_JDM_REQUIRED", "JDM must be an object")
    if "when" in content:
        raise StandardJdmError(
            "STANDARD_JDM_REQUIRED",
            "legacy conditions are not executable",
        )
    if "actions" in content:
        raise StandardJdmError(
            "JDM_SIDE_EFFECT_FORBIDDEN",
            "JDM cannot carry side-effect actions",
        )
    private_config = content.get("_config")
    if isinstance(private_config, Mapping) and private_config.get("actions"):
        raise StandardJdmError(
            "JDM_SIDE_EFFECT_FORBIDDEN",
            "JDM cannot carry side-effect actions",
        )
    nodes = content.get("nodes")
    edges = content.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list) or not nodes:
        raise StandardJdmError(
            "STANDARD_JDM_REQUIRED",
            "standard JDM requires non-empty nodes and an edges array",
        )
    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, Mapping):
            raise StandardJdmError("JDM_NODE_INVALID", "every node must be an object")
        node_id = node.get("id")
        node_type = node.get("type")
        if not isinstance(node_id, str) or not node_id or node_id in node_ids:
            raise StandardJdmError("JDM_NODE_INVALID", "node IDs must be unique")
        if node_type not in ALLOWED_NODE_TYPES:
            raise StandardJdmError(
                "JDM_NODE_TYPE_FORBIDDEN",
                f"node type {node_type!r} is not allowed",
            )
        node_ids.add(node_id)
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise StandardJdmError("JDM_EDGE_INVALID", "every edge must be an object")
        if edge.get("sourceId") not in node_ids or edge.get("targetId") not in node_ids:
            raise StandardJdmError(
                "JDM_EDGE_INVALID",
                "edge endpoints must refer to graph nodes",
            )
    prepared = copy.deepcopy(dict(content))
    prepared.pop("_config", None)
    return prepared


def _create_decision(content: dict[str, object]):
    if _zen is None:
        raise StandardJdmError(
            "ZEN_ENGINE_UNAVAILABLE",
            "standard JDM requires zen-engine",
        )
    try:
        return _zen.ZenEngine().create_decision(content)
    except StandardJdmError:
        raise
    except Exception as error:
        raise StandardJdmError("JDM_COMPILE_FAILED", str(error)) from error


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
