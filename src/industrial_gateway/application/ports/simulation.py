"""Business port for applying an authorized simulation input."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from industrial_gateway.application.variable_store import Scalar


@dataclass(frozen=True, slots=True)
class SimulationInputReceipt:
    accepted: bool
    effective_step: int
    message: str
    error_code: str


class SimulationInputPort(Protocol):
    def start(self) -> None: ...

    async def submit(
        self,
        *,
        device_id: str,
        point_id: str,
        value: Scalar,
        request_id: str,
        timeout: float,
    ) -> SimulationInputReceipt: ...

    async def close(self) -> None: ...
