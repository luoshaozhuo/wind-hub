"""Transport-neutral Gateway calls consumed by Data Generator output policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from data_generator.domain.model import Scalar


@dataclass(frozen=True, slots=True)
class GatewayVariable:
    variable_id: str
    data_type: str
    writable: bool


@dataclass(frozen=True, slots=True)
class VariablePage:
    variables: tuple[GatewayVariable, ...]
    next_token: str


@dataclass(frozen=True, slots=True)
class GatewayLease:
    accepted: bool
    token: str
    error_code: str


@dataclass(frozen=True, slots=True)
class GatewayVariableResult:
    variable_id: str
    success: bool
    error_code: str
    message: str


class GatewayCallError(RuntimeError):
    """Transport error classified by canonical gRPC status name."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


class GatewayRpcPort(Protocol):
    async def list_variables(
        self,
        endpoint: str,
        gateway_id: str,
        page_token: str,
        timeout: float,
    ) -> VariablePage: ...

    async def claim(
        self,
        endpoint: str,
        gateway_id: str,
        owner_id: str,
        names: tuple[str, ...],
        action: str,
        token: str,
        lease_ms: int,
        timeout: float,
    ) -> GatewayLease: ...

    async def update(
        self,
        endpoint: str,
        gateway_id: str,
        token: str,
        values: tuple[tuple[str, Scalar], ...],
        timeout: float,
    ) -> tuple[GatewayVariableResult, ...]: ...

    async def close(self) -> None: ...
