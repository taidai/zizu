"""
TDD tests for alarm center enhancement — fault code matching, message resolution,
alarm_type/threshold, and count dedup.

Seams under test:
  - match_fault_entry(value, entries) — pure function, no DB
  - build_alarm_message(tag_name, alarm_level, alarm_type, threshold, value, entries) — pure
  - is_alarm_active(value) — pure
"""
from __future__ import annotations

import pytest

from app.services.alarm_logic import (
    match_fault_entry,
    build_alarm_message,
    is_alarm_active,
    ALARM_TYPE_OVER_TEMP,
    ALARM_TYPE_OVER_VOLTAGE,
    ALARM_TYPE_INSULATION,
    ALARM_TYPE_ANTI_ISLANDING,
    ALARM_TYPE_FIRE,
    ALARM_TYPE_COMMUNICATION,
    ALARM_TYPE_SOC_LIMIT,
    ALARM_TYPE_PROTECTION,
    STANDARD_ALARM_TYPES,
)


# ── match_fault_entry ──────────────────────────────────────────

class TestMatchFaultEntry:
    """Fault code matching: exact, hex, wildcard."""

    def test_exact_match(self):
        entries = [{"code": "1", "message": "过温"}]
        assert match_fault_entry(1, entries) == "过温"

    def test_exact_string_match(self):
        entries = [{"code": "16", "message": "IGBT过温"}]
        assert match_fault_entry("16", entries) == "IGBT过温"

    def test_hex_to_decimal_match(self):
        """Device sends 0x10, fault map has 16."""
        entries = [{"code": "16", "message": "直流过压"}]
        assert match_fault_entry("0x10", entries) == "直流过压"

    def test_decimal_to_hex_match(self):
        """Device sends 16, fault map has 0x10."""
        entries = [{"code": "0x10", "message": "直流过压"}]
        assert match_fault_entry(16, entries) == "直流过压"

    def test_wildcard_match(self):
        """Entries with code '*' match any non-empty value."""
        entries = [{"code": "*", "message": "通用故障"}]
        assert match_fault_entry(1, entries) == "通用故障"
        assert match_fault_entry("anything", entries) == "通用故障"

    def test_no_match_returns_none(self):
        entries = [{"code": "99", "message": "其他"}]
        assert match_fault_entry(1, entries) is None

    def test_empty_entries_returns_none(self):
        assert match_fault_entry(1, []) is None
        assert match_fault_entry(1, None) is None

    def test_first_match_wins(self):
        entries = [
            {"code": "1", "message": "过温"},
            {"code": "1", "message": "重复"},
        ]
        assert match_fault_entry(1, entries) == "过温"

    def test_string_value_with_whitespace(self):
        entries = [{"code": "1", "message": "故障"}]
        assert match_fault_entry(" 1 ", entries) == "故障"


# ── build_alarm_message ────────────────────────────────────────

class TestBuildAlarmMessage:
    """Message resolution with alarm_type, threshold, fault_map."""

    def test_fault_code_mode_with_type_and_fault_map(self):
        """Fault-code tag: value=1 matches code 1, fault_map translates."""
        entries = [{"code": "1", "message": "电池过温"}]
        msg = build_alarm_message(
            tag_name="IGBT温度",
            alarm_level="error1",
            alarm_type=ALARM_TYPE_OVER_TEMP,
            threshold=None,
            value=1,
            entries=entries,
        )
        assert "电池过温" in msg
        assert "过温" in msg

    def test_threshold_mode_with_type(self):
        """Threshold tag: value is a measurement, threshold determines active."""
        msg = build_alarm_message(
            tag_name="IGBT温度",
            alarm_level="error1",
            alarm_type=ALARM_TYPE_OVER_TEMP,
            threshold=55.0,
            value=58.3,
            entries=[],
        )
        assert "过温" in msg
        assert "55" in msg  # threshold
        assert "58" in msg  # actual value

    def test_without_fault_map_falls_back_to_type(self):
        msg = build_alarm_message(
            tag_name="SOC",
            alarm_level="error2",
            alarm_type=ALARM_TYPE_SOC_LIMIT,
            threshold=20.0,
            value=15.0,
            entries=[],
        )
        assert "SOC超限" in msg or "SOC" in msg
        assert "20" in msg

    def test_without_type_uses_level_and_tagname(self):
        msg = build_alarm_message(
            tag_name="IGBT温度",
            alarm_level="error1",
            alarm_type=None,
            threshold=None,
            value=35,
            entries=[],
        )
        assert "IGBT温度" in msg
        assert "35" in msg

    def test_string_fault_value_shows_message(self):
        entries = [{"code": "OV_FAULT", "message": "直流侧过压保护"}]
        msg = build_alarm_message(
            tag_name="PCS故障码",
            alarm_level="error1",
            alarm_type=ALARM_TYPE_OVER_VOLTAGE,
            threshold=None,
            value="OV_FAULT",
            entries=entries,
        )
        assert "直流侧过压保护" in msg


# ── is_alarm_active ─────────────────────────────────────────────

class TestIsAlarmActive:
    def test_nonzero_int_is_active(self):
        assert is_alarm_active(1) is True
        assert is_alarm_active(0) is False

    def test_nonzero_float_is_active(self):
        assert is_alarm_active(1.5) is True
        assert is_alarm_active(0.0) is False

    def test_true_bool_is_active(self):
        assert is_alarm_active(True) is True
        assert is_alarm_active(False) is False

    def test_nonempty_string_is_active(self):
        assert is_alarm_active("OV_FAULT") is True
        assert is_alarm_active("0") is False
        assert is_alarm_active("false") is False
        assert is_alarm_active("") is False
        assert is_alarm_active("off") is False

    def test_none_is_inactive(self):
        assert is_alarm_active(None) is False


# ── STANDARD_ALARM_TYPES ────────────────────────────────────────

class TestStandardAlarmTypes:
    def test_contains_all_required_types(self):
        required = {
            ALARM_TYPE_OVER_TEMP,
            ALARM_TYPE_OVER_VOLTAGE,
            ALARM_TYPE_INSULATION,
            ALARM_TYPE_ANTI_ISLANDING,
            ALARM_TYPE_FIRE,
            ALARM_TYPE_COMMUNICATION,
            ALARM_TYPE_SOC_LIMIT,
            ALARM_TYPE_PROTECTION,
        }
        assert required.issubset(STANDARD_ALARM_TYPES)

    def test_types_are_nonempty_strings(self):
        for t in STANDARD_ALARM_TYPES:
            assert isinstance(t, str)
            assert len(t) > 0

