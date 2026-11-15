"""Technology-neutral Gateway runtime composition ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from industrial_gateway.application.forwarding import CommandQuery, ForwardRecord
from industrial_gateway.application.variable_store import Scalar, VariableStore


class ResolvedServerPort(Protocol):
    @property
    def settings(self) -> Any: ...
    @property
    def protocol(self) -> str: ...
    @property
    def variables(self) -> Mapping[str, Any]: ...
    @property
    def unit_id(self) -> int | None: ...
    @property
    def registers(self) -> tuple[Any, ...]: ...


class ResolvedGatewayPort(Protocol):
    @property
    def service(self) -> Any: ...
    @property
    def servers(self) -> Mapping[str, ResolvedServerPort]: ...
    @property
    def revision(self) -> str: ...
    @property
    def project_root(self) -> Path: ...


class ProtocolListenerPort(Protocol):
    @property
    def is_serving(self) -> bool: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class ForwarderPort(Protocol):
    @property
    def pending(self) -> int: ...
    @property
    def records(self) -> tuple[ForwardRecord, ...]: ...

    def capture(
        self,
        *,
        event_id: str,
        gateway_id: str,
        device_id: str,
        point_id: str,
        value: Scalar,
        revision: str,
    ) -> bool: ...

    def pending_for(self, gateway_id: str) -> int: ...
    async def get_command_result(self, command_id: str) -> CommandQuery: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class GatewayObservabilityPort(Protocol):
    def record(self, gateway_id: str, metric: str, value: int = 1) -> None: ...
    def pending(self, gateway_id: str, count: int) -> None: ...
    def latency(self, gateway_id: str, duration_ms: float) -> None: ...
    @property
    def ready(self) -> bool: ...
    def metrics(self, scope: str, window_ms: int) -> Any: ...
    def query_events(
        self,
        *,
        filter_text: str = "",
        page_size: int = 100,
        page_token: str = "",
        from_time: datetime | None = None,
        to_time: datetime | None = None,
    ) -> Any: ...
    def event(
        self,
        source: str,
        message: str,
        *,
        severity: str = "INFO",
        error_code: str = "",
    ) -> None: ...
    def start(self, gateway_ids: Callable[[], tuple[str, ...]]) -> None: ...
    async def stop(self) -> None: ...


ListenerFactory = Callable[
    [str, ResolvedServerPort, VariableStore, Callable[[str, Scalar], Awaitable[None]]],
    ProtocolListenerPort | None,
]
ForwarderFactory = Callable[
    [str, ResolvedGatewayPort, Callable[[ForwardRecord], None]], ForwarderPort
]
