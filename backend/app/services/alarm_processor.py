"""Legacy MQTT alarm writer removed by ADR-0004 Ticket 13.

MQTT observations now enter ``TagAlarmAdapter`` and ``AlarmRuntime``.  This
module intentionally exposes no compatibility writer, so callers cannot
recreate a second lifecycle over ``t_alarms``.
"""
