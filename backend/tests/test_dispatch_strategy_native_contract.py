"""Native editor -> real GoRules -> official simulation contract regressions."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import unittest
from uuid import UUID

from app.services.dispatch_strategies import (
    EntityBindingContract, OutputBinding, StrategyBindingDraft, StrategyModelError,
    StrategyRuntime, StrategySnapshot, extract_control_intents, static_jdm_targets,
    validate_publish_bindings,
)
from app.services.gorules_adapter import evaluate_standard_jdm
from tests.test_dispatch_strategy_runtime import (
    NOW, OUTPUT_ID, REVISION_ID, SOC_ID, STRATEGY_ID, _Repository, _sample,
)


SECOND_OUTPUT = UUID("71000000-0000-0000-0000-000000000005")


def native_graph():
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    result = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e",
         "import {buildGenericDecisionTableJdm} from './src/components/dispatch-strategy/nativeDecisionTableModel.ts';"
         "console.log(JSON.stringify(buildGenericDecisionTableJdm()))"],
        cwd=frontend, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def explicit_graph(*, native_ids=False, output_path=True):
    action, target = ("column-action", "column-target") if native_ids else ("action_id", "target")
    table = {
        "hitPolicy": "collect", "inputs": [],
        "outputs": [{"id": action, "field": "action_id", "name": "Output alias"}, {"id": target, "field": "target", "name": "Target"}],
        "rules": [
            {"_id": "first", action: '"fan_enable"', target: "true"},
            {"_id": "second", action: '"pump_speed"', target: "12.5"},
        ],
    }
    if output_path:
        table["outputPath"] = "intents"
    return {
        "nodes": [{"id": "input", "type": "inputNode", "name": "Input"},
                  {"id": "table", "type": "decisionTableNode", "name": "Rules", "content": table},
                  {"id": "output", "type": "outputNode", "name": "Output"}],
        "edges": [{"id": "in", "sourceId": "input", "targetId": "table"},
                  {"id": "out", "sourceId": "table", "targetId": "output"}],
    }


def bindings_and_contracts():
    bindings = (
        StrategyBindingDraft("INPUT", "temperature", 0, SOC_ID, "FLOAT", "C", 10),
        StrategyBindingDraft("OUTPUT", "pump_speed", 0, SECOND_OUTPUT, "FLOAT", "Hz", 10),
        StrategyBindingDraft("OUTPUT", "fan_enable", 1, OUTPUT_ID, "BOOL", None, 10),
    )
    contracts = {
        SOC_ID: EntityBindingContract(True, "FLOAT", "C", "R", 0, None, None),
        OUTPUT_ID: EntityBindingContract(True, "BOOL", None, "RW", 1, None, None),
        SECOND_OUTPUT: EntityBindingContract(True, "FLOAT", "Hz", "RW", 1, 10, 20),
    }
    return bindings, contracts


def intermediate_target_graph(*, target="12.5", action='"pump_speed"'):
    return {
        "nodes": [
            {"id": "input", "type": "inputNode", "name": "Input"},
            {"id": "table", "type": "decisionTableNode", "name": "Intermediate", "content": {
                "hitPolicy": "first", "inputs": [],
                "outputs": [{"id": "target", "field": "target"}],
                "rules": [{"_id": "r", "target": target}],
            }},
            {"id": "bind", "type": "expressionNode", "name": "Static binding", "content": {
                "expressions": [{"id": "a", "key": "action_id", "value": action},
                                {"id": "t", "key": "target", "value": "target"}],
            }},
            {"id": "output", "type": "outputNode", "name": "Output"},
        ],
        "edges": [{"id": "e1", "sourceId": "input", "targetId": "table"},
                  {"id": "e2", "sourceId": "table", "targetId": "bind"},
                  {"id": "e3", "sourceId": "bind", "targetId": "output"}],
    }


class NativeContractTest(unittest.TestCase):
    def test_intermediate_target_table_keeps_single_output_full_graph_executable(self):
        graph = intermediate_target_graph()
        before = deepcopy(graph)
        outputs = evaluate_standard_jdm(graph, {})
        self.assertEqual({"action_id": "pump_speed", "target": 12.5}, outputs["result"])
        bindings, contracts = bindings_and_contracts()
        bindings = tuple(b for b in bindings if b.binding_key != "fan_enable")
        validate_publish_bindings(bindings, contracts, static_targets=static_jdm_targets(graph))
        result, repository = self.simulate(graph)
        repository.model = replace(repository.model, bindings=bindings)
        result = StrategyRuntime(repository).simulate(REVISION_ID, {}, NOW)
        self.assertEqual([("pump_speed", SECOND_OUTPUT, 12.5, 0)], [
            (i.action_id, i.entity_instance_id, i.value, i.ordinal) for i in result.intents
        ])
        self.assertFalse(result.persisted)
        self.assertEqual([], repository.mutations)
        self.assertEqual(before, graph)

    def test_intermediate_target_table_does_not_guess_between_multiple_outputs(self):
        bindings, contracts = bindings_and_contracts()
        with self.assertRaisesRegex(StrategyModelError, "OUTPUT_TARGET_AMBIGUOUS"):
            validate_publish_bindings(bindings, contracts, static_targets=static_jdm_targets(intermediate_target_graph()))

    def test_intermediate_targets_still_enforce_limits_type_and_literal_only(self):
        bindings, contracts = bindings_and_contracts()
        bindings = tuple(b for b in bindings if b.binding_key != "fan_enable")
        for target, code in [("9", "OUTPUT_LIMIT_VIOLATION"), ("21", "OUTPUT_LIMIT_VIOLATION"),
                             ("true", "OUTPUT_TYPE_MISMATCH"), ("temperature", "OUTPUT_TARGET_NOT_STATIC")]:
            with self.subTest(target=target):
                with self.assertRaisesRegex(StrategyModelError, code):
                    validate_publish_bindings(bindings, contracts, static_targets=static_jdm_targets(intermediate_target_graph(target=target)))

    def test_full_graph_unknown_action_remains_rejected_by_real_runtime(self):
        for action in ('"missing"', "temperature"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(StrategyModelError, "OUTPUT_BINDING_MISSING"):
                    self.simulate(intermediate_target_graph(action=action))

    def test_two_charge_two_discharge_matches_real_time_and_soc_boundaries(self):
        repository = _Repository()
        graph = repository.model.jdm_content
        frontend = Path(__file__).resolve().parents[2] / "frontend"
        output = subprocess.run([
            "node", "--input-type=module", "-e",
            "import {buildTwoChargeTwoDischargeJdm} from './src/components/dispatch-strategy/dispatchStrategyModel.mjs';"
            "console.log(JSON.stringify(buildTwoChargeTwoDischargeJdm([{key:'discharge-1',start:'10:00',end:'12:00',action:'DISCHARGE',target:156.7,socMin:40,socMax:90}],0)))",
        ], cwd=frontend, capture_output=True, text=True, check=True)
        for model in (graph, json.loads(output.stdout)):
            for minute, soc, expected in [(600, 40, 156.7), (630, 49, 156.7), (719, 90, 156.7),
                                          (599, 49, 0), (720, 49, 0), (630, 39, 0), (630, 91, 0)]:
                with self.subTest(minute=minute, soc=soc):
                    result = evaluate_standard_jdm(model, {"soc": soc, "site_local_minute": minute})["result"]
                    self.assertEqual(expected, result["target"])

    def test_existing_two_charge_two_discharge_safe_rule_remains_executable(self):
        repository = _Repository()
        graph = deepcopy(repository.model.jdm_content)
        result = StrategyRuntime(repository).simulate(REVISION_ID, {"soc": 20}, NOW)
        self.assertEqual([("power-target", 0.0)], [(i.action_id, i.value) for i in result.intents])
        self.assertEqual(graph, repository.model.jdm_content)

    def test_new_frontend_scaffold_evaluates_through_real_runtime_without_writes(self):
        graph = native_graph()
        table = graph["nodes"][1]["content"]
        table["rules"] = [{"_id": "fan", "action_id": '"fan_enable"', "target": "true"}]
        result, repository = self.simulate(graph)
        self.assertEqual([("fan_enable", OUTPUT_ID, True, 0)], [
            (i.action_id, i.entity_instance_id, i.value, i.ordinal) for i in result.intents
        ])
        self.assertFalse(result.persisted)
        self.assertEqual([], repository.mutations)

    def test_legacy_business_output_shape_is_rejected_not_silently_rewritten(self):
        graph = explicit_graph()
        table = graph["nodes"][1]["content"]
        table.update(hitPolicy="first", outputPath="", outputs=[{"id": "fan", "field": "fan_enable", "name": "Fan"}],
                     rules=[{"_id": "fan", "fan": "true"}])
        before = deepcopy(graph)
        outputs = evaluate_standard_jdm(graph, {})
        self.assertEqual({"fan_enable": True}, outputs["result"])
        with self.assertRaisesRegex(StrategyModelError, "OUTPUT_BINDING_MISSING"):
            extract_control_intents(outputs, {"fan_enable": OutputBinding("fan_enable", OUTPUT_ID, "BOOL", None, True, True)})
        self.assertEqual(before, graph)

    def test_multiple_outputs_publish_then_simulate_in_rule_order_not_binding_order(self):
        graph = explicit_graph()
        before = deepcopy(graph)
        bindings, contracts = bindings_and_contracts()
        validate_publish_bindings(bindings, contracts, static_targets=static_jdm_targets(graph))
        result, repository = self.simulate(graph)
        self.assertEqual([("fan_enable", OUTPUT_ID, True, 0), ("pump_speed", SECOND_OUTPUT, 12.5, 1)], [
            (i.action_id, i.entity_instance_id, i.value, i.ordinal) for i in result.intents
        ])
        self.assertEqual(before, graph)
        self.assertFalse(result.persisted)
        self.assertEqual([], repository.mutations)

    def test_full_graph_collect_result_uses_the_same_intent_contract(self):
        result, _ = self.simulate(explicit_graph(output_path=False))
        self.assertEqual([True, 12.5], [i.value for i in result.intents])

    def test_two_numeric_outputs_do_not_require_one_ambiguous_binding(self):
        graph = explicit_graph()
        graph["nodes"][1]["content"]["rules"][0]["target"] = "1"
        bindings, contracts = bindings_and_contracts()
        bindings = tuple(replace(b, expected_data_type="INT") if b.binding_key == "fan_enable" else b for b in bindings)
        contracts[OUTPUT_ID] = replace(contracts[OUTPUT_ID], data_type="INT", minimum=0, maximum=1)
        validate_publish_bindings(bindings, contracts, static_targets=static_jdm_targets(graph))

    def test_native_column_ids_cannot_hide_targets_from_publish_limits(self):
        graph = explicit_graph(native_ids=True)
        graph["nodes"][1]["content"]["rules"][1]["column-target"] = "21"
        bindings, contracts = bindings_and_contracts()
        with self.assertRaisesRegex(StrategyModelError, "OUTPUT_LIMIT_VIOLATION"):
            validate_publish_bindings(bindings, contracts, static_targets=static_jdm_targets(graph))

    def test_publish_checks_each_action_type_limits_and_static_identity(self):
        cases = [
            (0, "target", "1", "OUTPUT_TYPE_MISMATCH"),
            (1, "target", "true", "OUTPUT_TYPE_MISMATCH"),
            (1, "target", "9", "OUTPUT_LIMIT_VIOLATION"),
            (1, "target", "21", "OUTPUT_LIMIT_VIOLATION"),
            (0, "action_id", '"missing"', "OUTPUT_BINDING_MISSING"),
            (0, "action_id", "temperature", "OUTPUT_ACTION_NOT_STATIC"),
            (1, "target", "temperature + 1", "OUTPUT_TARGET_NOT_STATIC"),
        ]
        bindings, contracts = bindings_and_contracts()
        for row, field, value, code in cases:
            with self.subTest(value=value, code=code):
                graph = explicit_graph()
                graph["nodes"][1]["content"]["rules"][row][field] = value
                with self.assertRaisesRegex(StrategyModelError, code):
                    validate_publish_bindings(bindings, contracts, static_targets=static_jdm_targets(graph))

    def test_output_unit_mismatch_remains_fail_closed(self):
        bindings, contracts = bindings_and_contracts()
        contracts[SECOND_OUTPUT] = replace(contracts[SECOND_OUTPUT], unit="kW")
        with self.assertRaisesRegex(StrategyModelError, "L2_BINDING_UNIT_MISMATCH"):
            validate_publish_bindings(bindings, contracts, static_targets=static_jdm_targets(explicit_graph()))

    def simulate(self, graph):
        repository = _Repository()
        bindings, _ = bindings_and_contracts()
        repository.model = replace(repository.model, jdm_content=graph, bindings=bindings)
        repository.snapshot = StrategySnapshot(42, 7, NOW, (
            _sample("temperature", SOC_ID, 30, unit="C"),
            _sample("fan_enable", OUTPUT_ID, False, data_type="BOOL"),
            _sample("pump_speed", SECOND_OUTPUT, 10.0, unit="Hz"),
        ))
        return StrategyRuntime(repository).simulate(REVISION_ID, {}, NOW), repository


class NativeContractPublicApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_gorules_simulation_api_returns_two_ordered_proposed_intents(self):
        from fastapi import FastAPI
        from tests.test_dispatch_strategy_public_api import _view
        from tests.api_test_client import AuthenticatedApiClient
        from app.api import dispatch_strategies

        _, repository = NativeContractTest().simulate(explicit_graph())
        now = datetime.now(UTC)
        repository.snapshot = replace(repository.snapshot, inputs=tuple(
            replace(sample, observed_at=now) for sample in repository.snapshot.inputs
        ))
        view = replace(_view(), id=STRATEGY_ID, draft=repository.model)
        repository.get_strategy = lambda strategy_id: view if strategy_id == STRATEGY_ID else None
        app = FastAPI()
        app.include_router(dispatch_strategies.router, prefix="/api/v1")
        app.dependency_overrides[dispatch_strategies.get_dispatch_strategy_repository] = lambda: repository
        async with AuthenticatedApiClient(app) as client:
            response = await client.post(f"/api/v1/dispatch-strategies/{STRATEGY_ID}/simulate", json={})
        self.assertEqual(200, response.status_code, response.text)
        proposed = response.json()["proposed_intents"]
        self.assertEqual(["fan_enable", "pump_speed"], [i["action_id"] for i in proposed])
        self.assertEqual([str(OUTPUT_ID), str(SECOND_OUTPUT)], [i["entity_instance_id"] for i in proposed])
        self.assertEqual([True, 12.5], [i["value"] for i in proposed])
        self.assertEqual([0, 1], [i["ordinal"] for i in proposed])
        self.assertEqual([], repository.mutations)
