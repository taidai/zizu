from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import unittest

from app.services.dispatch_strategies import (
    EntityBindingContract,
    StrategyModelError,
    StrategyRuntime,
    StrategyTrigger,
    validate_publish_bindings,
)
from tests.test_dispatch_strategy_runtime import (
    NOW, OUTPUT_ID, REVISION_ID, SOC_ID, STRATEGY_ID,
    _Repository, _engine, _revision,
)


def with_metadata(item, **changes):
    return replace(item, **changes)


class DispatchStrategyBindingRolesTest(unittest.TestCase):
    def contracts(self, *, definition_id="bms.soc", data_type="FLOAT", unit="%"):
        return {
            SOC_ID: with_metadata(
                EntityBindingContract(True, data_type, unit, "R", 0, None, None),
                definition_id=definition_id,
            ),
            OUTPUT_ID: EntityBindingContract(True, "FLOAT", "kW", "RW", 1, 0, 200),
        }

    def test_publish_rejects_role_mismatch_even_when_declared_metadata_matches(self):
        cases = (
            ("room.humidity", "FLOAT", "%", "SOC_BINDING_DEFINITION_INVALID"),
            ("ess.soc", "FLOAT", "%", "SOC_BINDING_DEFINITION_INVALID"),
            ("soc", "FLOAT", "%", "SOC_BINDING_DEFINITION_INVALID"),
            ("bms.soc", "FLOAT", "kW", "SOC_BINDING_UNIT_INVALID"),
            ("bms.soc", "FLOAT", "degC", "SOC_BINDING_UNIT_INVALID"),
            ("bms.soc", "FLOAT", "1", "SOC_BINDING_UNIT_INVALID"),
            ("bms.soc", "FLOAT", None, "SOC_BINDING_UNIT_INVALID"),
            ("bms.soc", "BOOL", "%", "SOC_BINDING_TYPE_INVALID"),
        )
        for definition_id, data_type, unit, code in cases:
            with self.subTest(definition=definition_id, data_type=data_type, unit=unit):
                bindings = _revision().bindings
                bindings = (replace(bindings[0], expected_data_type=data_type, unit=unit), bindings[1])
                with self.assertRaisesRegex(StrategyModelError, code):
                    validate_publish_bindings(
                        bindings, self.contracts(definition_id=definition_id, data_type=data_type, unit=unit),
                        static_targets=(156.7,),
                    )

    def test_publish_accepts_both_standard_soc_definitions_and_numeric_types(self):
        for definition_id in ("bms.soc", "storage.soc"):
            for data_type in ("FLOAT", "INT"):
                with self.subTest(definition=definition_id, data_type=data_type):
                    bindings = _revision().bindings
                    validate_publish_bindings(
                        (replace(bindings[0], expected_data_type=data_type), bindings[1]),
                        self.contracts(definition_id=definition_id, data_type=data_type), static_targets=(156.7,),
                    )

    def test_power_target_requires_numeric_kw_but_other_bindings_remain_generic(self):
        bindings = _revision().bindings
        for data_type, unit, code in (
            ("FLOAT", "MW", "POWER_TARGET_BINDING_UNIT_INVALID"),
            ("FLOAT", "%", "POWER_TARGET_BINDING_UNIT_INVALID"),
            ("BOOL", "kW", "POWER_TARGET_BINDING_TYPE_INVALID"),
        ):
            with self.subTest(data_type=data_type, unit=unit):
                contracts = self.contracts()
                contracts[OUTPUT_ID] = EntityBindingContract(True, data_type, unit, "RW", 1, None, None)
                with self.assertRaisesRegex(StrategyModelError, code):
                    validate_publish_bindings(
                        (bindings[0], replace(bindings[1], expected_data_type=data_type, unit=unit)),
                        contracts, static_targets=(),
                    )
        contracts = self.contracts(definition_id="room.humidity")
        contracts[OUTPUT_ID] = EntityBindingContract(True, "BOOL", None, "RW", 1, None, None)
        validate_publish_bindings(
            (replace(bindings[0], binding_key="humidity"), replace(bindings[1], binding_key="switch", expected_data_type="BOOL", unit=None)),
            contracts, static_targets=(),
        )

    def test_old_enabled_invalid_soc_bindings_block_without_jdm_or_intents(self):
        for definition_id, unit, code in (
            ("room.humidity", "%", "SOC_BINDING_DEFINITION_INVALID"),
            ("bms.soc", "kW", "SOC_BINDING_UNIT_INVALID"),
            ("bms.soc", "degC", "SOC_BINDING_UNIT_INVALID"),
            ("bms.soc", "1", "SOC_BINDING_UNIT_INVALID"),
        ):
            with self.subTest(definition=definition_id, unit=unit):
                repository = _Repository()
                repository.model = replace(repository.model, bindings=(replace(repository.model.bindings[0], unit=unit), repository.model.bindings[1]))
                repository.snapshot = replace(repository.snapshot, inputs=(with_metadata(repository.snapshot.inputs[0], definition_id=definition_id, unit=unit), repository.snapshot.inputs[1]))
                calls = []
                def engine(content, inputs):
                    calls.append(inputs)
                    return _engine(content, inputs)
                result = StrategyRuntime(repository, evaluator=engine).evaluate(
                    STRATEGY_ID, StrategyTrigger("DATA_CHANGE", "invalid-role", NOW, 42)
                )
                self.assertEqual(("BLOCKED", code, (), []), (result.status, result.reason_code, result.intents, calls))
                self.assertEqual((), repository.mutations[0].intents)

    def test_soc_runtime_values_are_real_finite_numbers_in_percentage_range(self):
        for value in (True, False, "49", None, float("nan"), float("inf"), Decimal("NaN"), -0.1, 100.1):
            with self.subTest(value=value):
                repository = _Repository()
                repository.snapshot = replace(repository.snapshot, inputs=(with_metadata(repository.snapshot.inputs[0], definition_id="bms.soc", value=value), repository.snapshot.inputs[1]))
                result = StrategyRuntime(repository, evaluator=lambda _content, _inputs: {"result": {"action_id": "power-target", "target": 156.7}}).evaluate(
                    STRATEGY_ID, StrategyTrigger("DATA_CHANGE", "invalid-value", NOW, 42)
                )
                self.assertEqual(("BLOCKED", "SOC_VALUE_INVALID", ()), (result.status, result.reason_code, result.intents))

    def test_simulation_override_must_not_bypass_soc_range(self):
        repository = _Repository()
        repository.snapshot = replace(repository.snapshot, inputs=(with_metadata(repository.snapshot.inputs[0], definition_id="bms.soc"), repository.snapshot.inputs[1]))
        runtime = StrategyRuntime(repository, evaluator=_engine)
        for value in (-1, 101, float("nan"), True, "50"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(StrategyModelError, "SOC_VALUE_INVALID"):
                    runtime.simulate(REVISION_ID, {"soc": value}, NOW)
        for value in (0, 50.5, 100):
            with self.subTest(value=value):
                result = runtime.simulate(REVISION_ID, {"soc": value}, NOW)
                self.assertEqual("EVALUATED", result.status)
                self.assertEqual(value, result.engine_inputs["soc"])
        self.assertEqual([], repository.mutations)

    def test_runtime_does_not_restrict_generic_inputs_to_soc(self):
        repository = _Repository()
        repository.model = replace(repository.model, bindings=(
            replace(repository.model.bindings[0], binding_key="humidity"),
            repository.model.bindings[1],
        ))
        repository.snapshot = replace(repository.snapshot, inputs=(
            replace(repository.snapshot.inputs[0], field_key="humidity", definition_id="room.humidity", value=130.0),
            repository.snapshot.inputs[1],
        ))
        runtime = StrategyRuntime(repository, evaluator=lambda _content, inputs: {"result": {"action_id": "power-target", "target": inputs["humidity"]}})
        result = runtime.simulate(REVISION_ID, {"humidity": 150.0}, NOW)
        self.assertEqual("EVALUATED", result.status)
        self.assertEqual(150.0, result.intents[0].value)
        self.assertEqual([], repository.mutations)
