from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import unittest
from uuid import UUID

from app.services.dispatch_strategies import (
    DispatchWindow,
    OutputBinding,
    StrategyModelError,
    build_two_charge_two_discharge_jdm,
    extract_control_intents,
    split_cross_midnight,
)


OUTPUT_ID = UUID("70000000-0000-0000-0000-000000000001")


def window(
    key: str,
    start: str,
    end: str,
    action: str,
    target: str,
    soc_min: str = "10",
    soc_max: str = "90",
) -> DispatchWindow:
    return DispatchWindow(
        key=key,
        start=start,
        end=end,
        action=action,
        target=Decimal(target),
        soc_min=Decimal(soc_min),
        soc_max=Decimal(soc_max),
    )


class DispatchStrategyModelTest(unittest.TestCase):
    def test_cross_midnight_window_is_split_into_two_non_overlapping_rows(self) -> None:
        rows = split_cross_midnight(window("charge-1", "22:00", "02:00", "CHARGE", "-50"))

        self.assertEqual(
            (("charge-1:late", "22:00", "24:00"), ("charge-1:early", "00:00", "02:00")),
            tuple((row.key, row.start, row.end) for row in rows),
        )

    def test_overlapping_windows_are_rejected(self) -> None:
        rows = (
            window("charge-1", "01:00", "03:00", "CHARGE", "-50"),
            window("charge-2", "02:00", "04:00", "CHARGE", "-40"),
        )
        with self.assertRaisesRegex(StrategyModelError, "DISPATCH_WINDOWS_OVERLAP"):
            build_two_charge_two_discharge_jdm(rows, Decimal("0"))

    def test_unsplit_cross_midnight_window_is_rejected_by_builder(self) -> None:
        rows = (window("charge-1", "22:00", "02:00", "CHARGE", "-50"),)
        with self.assertRaisesRegex(StrategyModelError, "CROSS_MIDNIGHT_MUST_BE_SPLIT"):
            build_two_charge_two_discharge_jdm(rows, Decimal("0"))

    def test_soc_lower_bound_cannot_exceed_upper_bound(self) -> None:
        rows = (window("charge-1", "01:00", "03:00", "CHARGE", "-50", "80", "20"),)
        with self.assertRaisesRegex(StrategyModelError, "SOC_RANGE_INVALID"):
            build_two_charge_two_discharge_jdm(rows, Decimal("0"))

    def test_other_time_safe_target_is_mandatory(self) -> None:
        rows = (window("charge-1", "01:00", "03:00", "CHARGE", "-50"),)
        with self.assertRaisesRegex(StrategyModelError, "SAFE_TARGET_REQUIRED"):
            build_two_charge_two_discharge_jdm(rows, None)

    def test_builder_returns_one_standard_jdm_graph(self) -> None:
        rows = (
            window("charge-1", "01:00", "03:00", "CHARGE", "-50"),
            window("discharge-1", "10:00", "12:00", "DISCHARGE", "50"),
        )
        model = build_two_charge_two_discharge_jdm(rows, Decimal("0"))

        self.assertEqual(["inputNode", "decisionTableNode", "outputNode"], [node["type"] for node in model["nodes"]])
        table = model["nodes"][1]["content"]
        self.assertEqual("first", table["hitPolicy"])
        self.assertEqual("other-time", table["rules"][-1]["_id"])
        self.assertEqual("0", table["rules"][-1]["target"])
        self.assertNotIn("when", model)
        self.assertNotIn("actions", model)

    def test_only_a_statically_bound_typed_set_intent_can_be_extracted(self) -> None:
        binding = OutputBinding(
            action_id="limit",
            entity_instance_id=OUTPUT_ID,
            data_type="FLOAT",
            unit="kW",
            controllable=True,
            confirmed_write_point=True,
        )
        intents = extract_control_intents(
            {"action_id": "limit", "target": 156.7},
            {"limit": binding},
        )

        self.assertEqual(1, len(intents))
        self.assertEqual(OUTPUT_ID, intents[0].entity_instance_id)
        self.assertEqual(156.7, intents[0].value)
        self.assertEqual(0, intents[0].ordinal)

    def test_unknown_or_uncontrollable_output_is_rejected(self) -> None:
        with self.assertRaisesRegex(StrategyModelError, "OUTPUT_BINDING_MISSING"):
            extract_control_intents({"action_id": "missing", "target": 1}, {})

        binding = OutputBinding(
            action_id="limit",
            entity_instance_id=OUTPUT_ID,
            data_type="FLOAT",
            unit="kW",
            controllable=False,
            confirmed_write_point=True,
        )
        with self.assertRaisesRegex(StrategyModelError, "OUTPUT_NOT_CONTROLLABLE"):
            extract_control_intents(
                {"action_id": "limit", "target": 1},
                {"limit": binding},
            )

    def test_integer_and_boolean_targets_do_not_coerce_each_other(self) -> None:
        integer = OutputBinding("int", OUTPUT_ID, "INT", None, True, True)
        boolean = OutputBinding("bool", OUTPUT_ID, "BOOL", None, True, True)

        with self.assertRaisesRegex(StrategyModelError, "OUTPUT_TYPE_MISMATCH"):
            extract_control_intents({"action_id": "int", "target": True}, {"int": integer})
        with self.assertRaisesRegex(StrategyModelError, "OUTPUT_TYPE_MISMATCH"):
            extract_control_intents({"action_id": "bool", "target": 1}, {"bool": boolean})


if __name__ == "__main__":
    unittest.main()
