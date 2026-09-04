from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.gorules_adapter import (
    StandardJdmError,
    compile_standard_jdm,
    evaluate_standard_jdm,
)


STANDARD_MODEL = {
    "nodes": [
        {"id": "input", "type": "inputNode", "name": "Input"},
        {"id": "output", "type": "outputNode", "name": "Output"},
    ],
    "edges": [
        {"id": "edge", "sourceId": "input", "targetId": "output", "type": "edge"}
    ],
}


class _Decision:
    def __init__(self, result: dict) -> None:
        self._result = result

    def evaluate(self, context: dict) -> dict:
        return {**self._result, "seen": context}


class _Engine:
    result = {"action_id": "hold", "target": 0}
    last_content: dict | None = None

    def create_decision(self, content: dict) -> _Decision:
        type(self).last_content = content
        return _Decision(type(self).result)


class _Zen:
    ZenEngine = _Engine


class GoRulesAdapterTest(unittest.TestCase):
    def test_standard_jdm_compiles_to_a_stable_digest(self) -> None:
        with patch("app.services.gorules_adapter._zen", _Zen):
            first = compile_standard_jdm(STANDARD_MODEL)
            second = compile_standard_jdm(
                {"edges": STANDARD_MODEL["edges"], "nodes": STANDARD_MODEL["nodes"]}
            )

        self.assertEqual(first, second)
        self.assertEqual(64, len(first))

    def test_standard_jdm_evaluates_with_the_real_context_boundary(self) -> None:
        with patch("app.services.gorules_adapter._zen", _Zen):
            result = evaluate_standard_jdm(STANDARD_MODEL, {"soc": 51})

        self.assertEqual("hold", result["action_id"])
        self.assertEqual({"soc": 51}, result["seen"])

    def test_missing_zen_engine_fails_explicitly(self) -> None:
        with patch("app.services.gorules_adapter._zen", None):
            with self.assertRaisesRegex(StandardJdmError, "ZEN_ENGINE_UNAVAILABLE"):
                compile_standard_jdm(STANDARD_MODEL)

    def test_simplified_when_actions_model_is_rejected(self) -> None:
        with patch("app.services.gorules_adapter._zen", _Zen):
            with self.assertRaisesRegex(StandardJdmError, "STANDARD_JDM_REQUIRED"):
                compile_standard_jdm({"when": "soc > 50", "actions": []})

    def test_side_effect_actions_are_rejected_even_on_a_standard_graph(self) -> None:
        model = {**STANDARD_MODEL, "actions": [{"type": "http", "url": "invalid"}]}
        with patch("app.services.gorules_adapter._zen", _Zen):
            with self.assertRaisesRegex(StandardJdmError, "JDM_SIDE_EFFECT_FORBIDDEN"):
                compile_standard_jdm(model)

    def test_engine_compile_error_is_not_reinterpreted(self) -> None:
        class BrokenEngine:
            def create_decision(self, content: dict):
                raise ValueError("bad graph")

        class BrokenZen:
            ZenEngine = BrokenEngine

        with patch("app.services.gorules_adapter._zen", BrokenZen):
            with self.assertRaisesRegex(StandardJdmError, "JDM_COMPILE_FAILED"):
                compile_standard_jdm(STANDARD_MODEL)


if __name__ == "__main__":
    unittest.main()
