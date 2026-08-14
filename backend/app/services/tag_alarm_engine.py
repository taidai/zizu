"""Legacy tag alarm writer removed by ADR-0004 Ticket 13.

Confirmed tag observations now enter ``TagAlarmAdapter`` and the unified
``AlarmRuntime``.  The historical module path remains only to fail closed for
accidental imports; it contains no write interface.
"""
