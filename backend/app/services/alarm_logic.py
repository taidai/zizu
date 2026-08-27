"""Legacy alarm labels retained for read-only history and API presentation."""
from __future__ import annotations

from typing import Any

# ── Standard alarm types (GB/T 36276, GB/T 19963, GB/T 51048) ──

ALARM_TYPE_OVER_TEMP = "过温"
ALARM_TYPE_OVER_VOLTAGE = "过压"
ALARM_TYPE_UNDER_VOLTAGE = "欠压"
ALARM_TYPE_OVER_CURRENT = "过流"
ALARM_TYPE_INSULATION = "绝缘降低"
ALARM_TYPE_COMMUNICATION = "通信中断"
ALARM_TYPE_SOC_LIMIT = "SOC超限"
ALARM_TYPE_SOH_LIMIT = "SOH超限"
ALARM_TYPE_HARMONIC = "谐波超限"
ALARM_TYPE_ANTI_ISLANDING = "防孤岛"
ALARM_TYPE_PROTECTION = "保护动作"
ALARM_TYPE_FIRE = "消防告警"
ALARM_TYPE_ARC_FAULT = "电弧故障"
ALARM_TYPE_EMERGENCY_STOP = "急停"
ALARM_TYPE_OTHER = "其他"

STANDARD_ALARM_TYPES: set[str] = {
    ALARM_TYPE_OVER_TEMP,
    ALARM_TYPE_OVER_VOLTAGE,
    ALARM_TYPE_UNDER_VOLTAGE,
    ALARM_TYPE_OVER_CURRENT,
    ALARM_TYPE_INSULATION,
    ALARM_TYPE_COMMUNICATION,
    ALARM_TYPE_SOC_LIMIT,
    ALARM_TYPE_SOH_LIMIT,
    ALARM_TYPE_HARMONIC,
    ALARM_TYPE_ANTI_ISLANDING,
    ALARM_TYPE_PROTECTION,
    ALARM_TYPE_FIRE,
    ALARM_TYPE_ARC_FAULT,
    ALARM_TYPE_EMERGENCY_STOP,
    ALARM_TYPE_OTHER,
}


def is_alarm_active(value: Any) -> bool:
    """Determine whether a tag value represents an active alarm state."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        stripped = value.strip().lower()
        return stripped != "" and stripped not in {"0", "false", "off", "no"}
    return bool(value)


def _code_str(value: Any) -> str:
    """Normalise a tag value to a fault-code string for matching."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value).strip()


def _try_parse_int(s: str) -> int | None:
    """Parse a string as decimal or hex int; return None on failure."""
    s = s.strip()
    if not s:
        return None
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s, 0) if s.startswith("0") and len(s) > 1 else int(s)
    except (ValueError, TypeError):
        return None


def match_fault_entry(value: Any, entries: list[dict] | None) -> str | None:
    """
    Match a tag value against fault-map entries.

    Matching priority:
      1. Exact string match (after whitespace strip)
      2. Numeric equivalence (hex 0x10 == 16)
      3. Wildcard '*' matches any non-empty value
    Returns the matched message, or None if no match.
    """
    if not entries:
        return None

    code = _code_str(value)
    if not code:
        return None

    code_int = _try_parse_int(code)

    for entry in entries:
        entry_code = str(entry.get("code", "")).strip()

        # 1. Exact string match
        if entry_code == code:
            return entry.get("message", entry_code)

        # 2. Numeric equivalence (hex/decimal cross-match)
        if code_int is not None:
            entry_int = _try_parse_int(entry_code)
            if entry_int is not None and entry_int == code_int:
                return entry.get("message", entry_code)

    # 3. Wildcard match — only if value is non-empty/active
    for entry in entries:
        if str(entry.get("code", "")).strip() == "*":
            return entry.get("message", "通用故障")

    return None


def build_alarm_message(
    tag_name: str,
    alarm_level: str,
    alarm_type: str | None,
    threshold: float | None,
    value: Any,
    entries: list[dict] | None,
) -> str:
    """
    Build a human-readable alarm message.

    Format: [level] alarm_type: fault_description (阈值=X 实际=Y)
    Falls back gracefully when alarm_type or fault_map is missing.
    """
    parts: list[str] = [f"[{alarm_level}]"]

    # Try fault-map resolution first
    fault_msg = match_fault_entry(value, entries)

    if alarm_type:
        parts.append(alarm_type)
        if fault_msg:
            parts.append(f": {fault_msg}")
    elif fault_msg:
        parts.append(fault_msg)
    else:
        parts.append(tag_name)
        parts.append("告警")

    # Append threshold + actual value for context
    value_str = _code_str(value)
    context_parts: list[str] = []
    if threshold is not None:
        context_parts.append(f"阈值{threshold}")
    if value_str:
        context_parts.append(f"实际{value_str}")
    if context_parts:
        parts.append(f" ({' '.join(context_parts)})")

    return "".join(parts)
