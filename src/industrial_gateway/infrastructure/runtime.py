"""Industrial Gateway concrete runtime composition: protocol and forwarding adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from industrial_gateway.application.forwarding import ForwardRecord
from industrial_gateway.application.ports.runtime import ResolvedGatewayPort, ResolvedServerPort
from industrial_gateway.application.runtime import GatewayApplicationRuntime
from industrial_gateway.application.variable_store import Scalar, VariableStore
from industrial_gateway.infrastructure.adapters.inbound.modbus.server import DeviceModbusServer
from industrial_gateway.infrastructure.adapters.outbound.forwarder import CommandForwarder
from industrial_gateway.infrastructure.config.resolution import ResolvedGateway
from industrial_gateway.infrastructure.observability import GatewayObservability


def _make_listener(
    server_id: str,
    resolved: ResolvedServerPort,
    store: VariableStore,
    on_write: Callable[[str, Scalar], Awaitable[None]],
) -> DeviceModbusServer | None:
    if resolved.protocol == "modbus":
        if resolved.unit_id is None:
            raise ValueError("Modbus Server has no effective Unit ID")
        return DeviceModbusServer(
            host=resolved.settings.listen_host,
            port=resolved.settings.listen_port,
            unit_id=resolved.unit_id,
            mappings=resolved.registers,
            store=store,
            on_write=on_write,
            allowed_writes=(
                frozenset(item.variable_id for item in resolved.settings.control.mappings)
                if resolved.settings.control.enabled
                else frozenset()
            ),
        )
    if resolved.settings.desired_state == "START":
        raise ValueError(f"{server_id}: adapter {resolved.protocol} is not implemented")
    return None


def _make_forwarder(
    server_id: str,
    config: ResolvedGatewayPort,
    observer: Callable[[ForwardRecord], None],
) -> CommandForwarder:
    return CommandForwarder(
        config.servers[server_id].settings.control.target,
        project_root=config.project_root,
        observer=observer,
    )


class GatewayRuntime(GatewayApplicationRuntime):
    """Concrete composition wrapper; Application owns all business state."""

    def __init__(self, config: ResolvedGateway) -> None:
        super().__init__(
            config,
            build_listener=_make_listener,
            build_forwarder=_make_forwarder,
            observability=GatewayObservability(
                window_seconds=config.service.metrics.window_seconds,
                interval_ms=config.service.metrics.collect_interval_ms,
            ),
        )
