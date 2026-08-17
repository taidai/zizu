"""L0—L2 数据主干的唯一公开写入模块。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from app.services.data_trunk_contracts import (
    CommitReceipt,
    DataTrunkError,
    InputReference,
    InstalledPointConversion,
    L2Observation,
    RawObservation,
)
from app.services.data_trunk_conversion import evaluate_conversion


class ConversionEvaluator(Protocol):
    def __call__(
        self,
        *,
        installed: tuple[InstalledPointConversion, ...],
        current_inputs: Mapping[InputReference, RawObservation | L2Observation],
        site_configuration_version: int,
        calculated_at: datetime,
    ) -> tuple[L2Observation, ...]: ...


class DataTrunkRepository(Protocol):
    def transact(
        self,
        raw_observations: tuple[RawObservation, ...],
        evaluator: ConversionEvaluator,
    ) -> CommitReceipt: ...


class DataTrunk:
    """隐藏转换查找、时序推进、来源图、outbox 与事务顺序。"""

    def __init__(self, repository: DataTrunkRepository) -> None:
        self._repository = repository

    def ingest(self, raw_observations: Sequence[RawObservation]) -> CommitReceipt:
        batch = tuple(raw_observations)
        if not batch:
            raise DataTrunkError(
                "DATA_TRUNK_BATCH_EMPTY",
                "Raw observation batch is empty",
            )
        return self._repository.transact(batch, evaluate_conversion)
