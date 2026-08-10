"""
TDD tests for entity_alarm_engine.
"""
from __future__ import annotations

import pytest

from app.services.entity_alarm_engine import (
    evaluate_trigger_rule,
    evaluate_trigger_rules,
    _extract_value,
)


class TestExtractValue:
    def test_extract_str(self):
        assert _extract_value({"value_str": "fault"}) == "fault"

    def test_extract_bool(self):
        assert _extract_value({"value_bool": True}) is True

    def test_extract_int(self):
        assert _extract_value({"value_int": 42}) == 42

    def test_extract_float(self):
        assert _extract_value({"value_float": 3.14}) == 3.14

    def test_priority_str_over_bool(self):
        assert _extract_value({"value_str": "x", "value_bool": True}) == "x"


class TestEvaluateTriggerRule:
    def test_active_true(self):
        assert evaluate_trigger_rule(True, {"op": "active"}, None) is True

    def test_active_false(self):
        assert evaluate_trigger_rule(0, {"op": "active"}, None) is False

    def test_active_string_zero(self):
        assert evaluate_trigger_rule("0", {"op": "active"}, None) is False

    def test_eq_match(self):
        assert evaluate_trigger_rule("1", {"op": "eq", "value": "1"}, None) is True

    def test_eq_mismatch(self):
        assert evaluate_trigger_rule("0", {"op": "eq", "value": "1"}, None) is False

    def test_ne(self):
        assert evaluate_trigger_rule("0", {"op": "ne", "value": "1"}, None) is True

    def test_gte_pass(self):
        assert evaluate_trigger_rule(110, {"op": "gte", "threshold": 100}, None) is True

    def test_gte_fail(self):
        assert evaluate_trigger_rule(90, {"op": "gte", "threshold": 100}, None) is False

    def test_gt_pass(self):
        assert evaluate_trigger_rule(101, {"op": "gt", "threshold": 100}, None) is True

    def test_lte_pass(self):
        assert evaluate_trigger_rule(10, {"op": "lte", "threshold": 10}, None) is True

    def test_lt_pass(self):
        assert evaluate_trigger_rule(9, {"op": "lt", "threshold": 10}, None) is True

    def test_numeric_on_string_returns_false(self):
        assert evaluate_trigger_rule("hot", {"op": "gte", "threshold": 100}, None) is False

    def test_fault_match(self):
        entries = [{"code": "0x01", "message": "过压"}]
        assert evaluate_trigger_rule("0x01", {"op": "fault"}, entries) is True

    def test_fault_no_match(self):
        entries = [{"code": "0x01", "message": "过压"}]
        assert evaluate_trigger_rule("0x02", {"op": "fault"}, entries) is False

    def test_unknown_op_fallback_to_active(self):
        assert evaluate_trigger_rule(1, {"op": "weird"}, None) is True


class TestEvaluateTriggerRules:
    def test_any_default(self):
        rules = [{"op": "eq", "value": "1"}, {"op": "gte", "threshold": 100}]
        assert evaluate_trigger_rules(110, rules, None) is True

    def test_all_mode(self):
        rules = [{"op": "gte", "threshold": 50}, {"op": "lte", "threshold": 100}]
        assert evaluate_trigger_rules(75, rules, None, match_mode="all") is True
        assert evaluate_trigger_rules(120, rules, None, match_mode="all") is False

    def test_empty_rules_fallback_active(self):
        assert evaluate_trigger_rules(1, [], None) is True
        assert evaluate_trigger_rules(0, [], None) is False
