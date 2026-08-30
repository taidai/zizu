"""Shared freshness policy for committed data-trunk observations."""
from __future__ import annotations

from datetime import datetime

from app.services.data_trunk_contracts import TrunkQuality


def effective_l0_quality(
    frame_sequence: int,
    *,
    has_value: bool,
    stored_quality: int | TrunkQuality,
    capture_beat: int = 0,
    accepted_beat: int | None = None,
    received_at: datetime | None = None,
    evaluated_at: datetime | None = None,
) -> TrunkQuality:
    """Apply the one L0 freshness rule used by projections and L1 trials."""
    if not has_value or frame_sequence == 0:
        return TrunkQuality.STALE
    effective = TrunkQuality(int(stored_quality))
    expired_by_beat = (
        accepted_beat is None
        or int(accepted_beat) <= 0
        or capture_beat - int(accepted_beat) >= 3
    )
    expired_by_time = (
        received_at is not None
        and evaluated_at is not None
        and (evaluated_at - received_at).total_seconds() >= 3
    )
    if expired_by_beat or expired_by_time:
        return min(effective, TrunkQuality.STALE)
    return effective
