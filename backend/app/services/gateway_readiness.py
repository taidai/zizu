"""受控协议网关的最小在线探针，不向交付报告泄露连接参数。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class GatewayReadinessResult:
    gateway: str
    status: str
    code: str

    def public_dict(self) -> dict[str, str]:
        return {"gateway": self.gateway, "status": self.status, "code": self.code}


class GatewayReadiness(Protocol):
    async def check(self) -> GatewayReadinessResult: ...


class NeuronGatewayReadiness:
    """Prove that ZiZu can authenticate to the configured Neuron gateway."""

    def __init__(self, client_factory: Callable[[], object]) -> None:
        self._client_factory = client_factory

    async def check(self) -> GatewayReadinessResult:
        try:
            client = self._client_factory()
            await asyncio.to_thread(client.get_version)  # type: ignore[attr-defined]
        except Exception:
            return GatewayReadinessResult("neuron", "unavailable", "GATEWAY_UNAVAILABLE")
        return GatewayReadinessResult("neuron", "connected", "GATEWAY_CONNECTED")
