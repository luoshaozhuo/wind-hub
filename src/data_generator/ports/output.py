"""Output port implemented by a bounded Gateway client adapter."""

from __future__ import annotations

from collections import deque
from typing import Protocol

from data_generator.domain.model import DataBatch, DeliveryResult
from data_generator.domain.scenario import Scenario


class OutputPort(Protocol):
    @property
    def unsafe_gateways(self) -> frozenset[str]: ...

    @property
    def unsettled_gateways(self) -> frozenset[str]: ...

    rpc_durations: deque[tuple[float, str, float]]

    def has_claims(self, owner_id: str) -> bool: ...

    async def validate(self, scenario: Scenario) -> None: ...

    async def claim(self, scenario: Scenario, owner_id: str) -> None: ...

    async def release(self, scenario: Scenario, owner_id: str) -> bool: ...

    async def send(self, scenario: Scenario, batch: DataBatch) -> list[DeliveryResult]: ...

    async def wait_settled(self, gateway_ids: frozenset[str], timeout: float) -> bool: ...

    async def drain(self) -> None: ...

    async def close(self) -> bool: ...
