"""Real JDM numeric literal and per-evaluation output identity boundaries."""
from copy import deepcopy
from dataclasses import replace
import unittest

from app.services.dispatch_strategies import (
    StrategyBindingDraft, StrategyModelError, StrategyRuntime, StrategySnapshot,
    StrategyTrigger, static_jdm_targets, validate_publish_bindings,
)
from app.services.gorules_adapter import evaluate_standard_jdm
from tests.test_dispatch_strategy_native_contract import (
    SECOND_OUTPUT, bindings_and_contracts, explicit_graph,
)
from tests.test_dispatch_strategy_runtime import (
    NOW, REVISION_ID, SOC_ID, STRATEGY_ID, _Repository, _sample,
)


def pump_graph(targets, *, aliases=None):
    graph = explicit_graph()
    graph["nodes"][1]["content"]["rules"] = [
        {"_id": f"row-{index}", "action_id": f'"{aliases[index] if aliases else "pump_speed"}"', "target": target}
        for index, target in enumerate(targets)
    ]
    return graph


def pump_repository(graph, *, alias_same_entity=False):
    bindings, _ = bindings_and_contracts()
    bindings = tuple(b for b in bindings if b.binding_key != "fan_enable")
    if alias_same_entity:
        bindings += (StrategyBindingDraft("OUTPUT", "pump_alt", 1, SECOND_OUTPUT, "FLOAT", "Hz", 10),)
    repository = _Repository()
    repository.model = replace(repository.model, jdm_content=graph, bindings=bindings)
    repository.snapshot = StrategySnapshot(42, 7, NOW, (
        _sample("temperature", SOC_ID, 30.0, unit="C"),
        _sample("pump_speed", SECOND_OUTPUT, 10.0, unit="Hz"),
    ))
    return repository


class StaticNumericLiteralTest(unittest.TestCase):
    def validate(self, graph, data_type):
        bindings, contracts = bindings_and_contracts()
        bindings = tuple(replace(b, expected_data_type=data_type) if b.binding_key == "pump_speed" else b
                         for b in bindings if b.binding_key != "fan_enable")
        contracts[SECOND_OUTPUT] = replace(contracts[SECOND_OUTPUT], data_type=data_type, minimum=0, maximum=2)
        validate_publish_bindings(bindings, contracts, static_targets=static_jdm_targets(graph))

    def test_integer_literal_spellings_match_real_gorules_and_publish(self):
        for literal in ("1", "1.0", "1e0", "+1", "01"):
            for data_type in ("INT", "FLOAT"):
                with self.subTest(literal=literal, data_type=data_type):
                    graph = pump_graph([literal])
                    before = deepcopy(graph)
                    result = evaluate_standard_jdm(graph, {})["result"]["intents"][0]
                    self.assertEqual({"action_id": "pump_speed", "target": 1}, result)
                    self.validate(graph, data_type)
                    self.assertEqual(before, graph)

    def test_nonintegral_target_stays_float_only(self):
        graph = pump_graph(["1.5"])
        self.assertEqual(1.5, evaluate_standard_jdm(graph, {})["result"]["intents"][0]["target"])
        self.validate(graph, "FLOAT")
        with self.assertRaisesRegex(StrategyModelError, "OUTPUT_TYPE_MISMATCH"):
            self.validate(graph, "INT")

    def test_nonfinite_dynamic_boolean_and_text_targets_cannot_become_numbers(self):
        for literal in ("NaN", "Infinity", "-Infinity", "1e9999", "temperature", "1 + 0", "true", '"1"'):
            for data_type in ("INT", "FLOAT"):
                with self.subTest(literal=literal, data_type=data_type):
                    with self.assertRaises(StrategyModelError):
                        self.validate(pump_graph([literal]), data_type)

    def test_normalized_literals_still_obey_limits(self):
        for literal in ("+3", "03", "3.0", "3e0", "-1"):
            with self.subTest(literal=literal):
                evaluate_standard_jdm(pump_graph([literal]), {})
                with self.assertRaisesRegex(StrategyModelError, "OUTPUT_LIMIT_VIOLATION"):
                    self.validate(pump_graph([literal]), "INT")

    def test_boolean_and_string_literals_remain_strict(self):
        for literal, data_type, target in (("true", "BOOL", True), ('"1.0"', "STRING", "1.0")):
            with self.subTest(data_type=data_type):
                graph = pump_graph([literal])
                self.assertEqual(target, evaluate_standard_jdm(graph, {})["result"]["intents"][0]["target"])
                self.validate(graph, data_type)
                with self.assertRaisesRegex(StrategyModelError, "OUTPUT_TYPE_MISMATCH"):
                    self.validate(pump_graph(["1.0"]), data_type)


class DuplicateOutputResultTest(unittest.TestCase):
    def test_duplicate_action_rejects_whole_result_before_control_intents(self):
        self.assert_rejected(pump_graph(["12.5", "13.5"]), "OUTPUT_ACTION_DUPLICATED")

    def test_distinct_actions_for_one_entity_reject_whole_result(self):
        self.assert_rejected(pump_graph(["12.5", "13.5"], aliases=["pump_speed", "pump_alt"]),
                             "OUTPUT_ENTITY_DUPLICATED", alias_same_entity=True)

    def assert_rejected(self, graph, code, *, alias_same_entity=False):
        result = evaluate_standard_jdm(graph, {})["result"]["intents"]
        self.assertEqual([12.5, 13.5], [row["target"] for row in result])
        repository = pump_repository(graph, alias_same_entity=alias_same_entity)
        runtime = StrategyRuntime(repository)
        with self.assertRaisesRegex(StrategyModelError, code):
            runtime.simulate(REVISION_ID, {}, NOW)
        self.assertEqual([], repository.mutations)
        result = runtime.evaluate(STRATEGY_ID, StrategyTrigger("DATA_CHANGE", "frame:42", NOW, 42))
        self.assertEqual("FAILED", result.status)
        self.assertEqual(code, result.reason_code)
        self.assertEqual((), result.intents)
        self.assertEqual(1, len(repository.mutations))
        self.assertEqual((), repository.mutations[0].intents)
        self.assertEqual(code, repository.mutations[0].failure_code)
        self.assertEqual(set(), repository.open_intents)

    def test_mutually_exclusive_rows_can_reuse_action_and_keep_one_intent(self):
        graph = pump_graph(["12.5", "13.5"])
        table = graph["nodes"][1]["content"]
        table["inputs"] = [{"id": "temp", "name": "Temperature", "field": "temperature"}]
        table["rules"][0]["temp"] = "< 30"
        table["rules"][1]["temp"] = ">= 30"
        repository = pump_repository(graph)
        _, contracts = bindings_and_contracts()
        validate_publish_bindings(repository.model.bindings, contracts, static_targets=static_jdm_targets(graph))
        for temperature, target in ((20, 12.5), (30, 13.5)):
            with self.subTest(temperature=temperature):
                result = StrategyRuntime(repository).simulate(REVISION_ID, {"temperature": temperature}, NOW)
                self.assertEqual([("pump_speed", SECOND_OUTPUT, target, 0)], [
                    (i.action_id, i.entity_instance_id, i.value, i.ordinal) for i in result.intents
                ])
        self.assertEqual([], repository.mutations)
