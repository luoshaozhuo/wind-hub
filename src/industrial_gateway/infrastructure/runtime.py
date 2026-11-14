"""Gateway v1 runtime ownership and per-server lifecycle coordination."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from industrial_gateway.application.server_lifecycle import (
    ActualState,
    DesiredState,
    ServerLifecycle,
)
from industrial_gateway.application.variable_store import (
    Scalar,
    UpdateResult,
    VariableStore,
)
from industrial_gateway.infrastructure.adapters.outbound.forwarder import (
    CommandForwarder,
    ForwardRecord,
    ForwardState,
)
from industrial_gateway.infrastructure.config.resolution import (
    ResolvedGateway,
    ResolvedServer,
)
from industrial_gateway.infrastructure.observability import GatewayObservability
from industrial_gateway.infrastructure.adapters.inbound.modbus.server import (
    DeviceModbusServer,
)
from industrial_gateway.infrastructure.variable_leases import (
    ClaimRequest,
    ClaimResult,
    VariableLeases,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApplicationResult:
    """One runtime configuration-apply outcome, independent of the gRPC wire."""

    object_id: str
    status: str
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class ProtocolWriteEvent:
    event_id: str
    gateway_id: str
    variable_id: str
    value: Scalar
    observed_at: datetime


@dataclass(slots=True)
class GatewayInstance:
    config: ResolvedServer
    lifecycle: ServerLifecycle
    store: VariableStore
    protocol_server: DeviceModbusServer | None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class GatewayRuntime:
    """Owns all Gateway listeners and state; no implicit physical PLC connections."""

    def __init__(self, config: ResolvedGateway) -> None:
        self.config = config
        self._process_service = config.service
        self.instances: dict[str, GatewayInstance] = {}
        self.observability = GatewayObservability(
            window_seconds=config.service.metrics.window_seconds,
            interval_ms=config.service.metrics.collect_interval_ms,
        )
        self.forwarders: dict[str, CommandForwarder] = {}
        for server_id, resolved in config.servers.items():
            control = resolved.settings.control
            if control.enabled:
                self.forwarders[server_id] = CommandForwarder(
                    control.target,
                    project_root=config.project_root,
                    observer=self._record_forward,
                )
        self.write_events: deque[ProtocolWriteEvent] = deque(maxlen=1024)
        self._configuration_lock = asyncio.Lock()
        self.variable_leases = VariableLeases()
        for server_id, resolved in config.servers.items():
            self.instances[server_id] = self._make_instance(server_id, resolved, config.revision)
        self._watchdog: asyncio.Task[None] | None = None
        self._closing = False

    def _record_forward(self, record: ForwardRecord) -> None:
        """Count each terminal forward transition and retain a bounded event."""
        self.observability.pending(
            record.gateway_id,
            self.forwarders[record.gateway_id].pending_for(record.gateway_id)
            if record.gateway_id in self.forwarders
            else 0,
        )
        if record.state is ForwardState.ACCEPTED:
            self.observability.record(record.gateway_id, "forward_accepted_total")
        elif record.state is ForwardState.UNKNOWN:
            self.observability.record(record.gateway_id, "forward_unknown_total")
        if record.state in {
            ForwardState.ACCEPTED,
            ForwardState.REJECTED,
            ForwardState.UNKNOWN,
            ForwardState.OVERLOADED,
        }:
            self.observability.event(
                record.gateway_id,
                (
                    f"forward {record.state.value}: {record.event_id}"
                    + (f" effective_step={record.effective_step}" if record.effective_step else "")
                ),
                severity=(
                    "WARNING"
                    if record.state in {ForwardState.UNKNOWN, ForwardState.OVERLOADED}
                    else "INFO"
                ),
                error_code=record.state.value,
            )

    def _make_instance(
        self,
        server_id: str,
        resolved: ResolvedServer,
        revision: str,
        previous: GatewayInstance | None = None,
    ) -> GatewayInstance:
        """Construct a listener and preserve compatible values on hot replacement."""
        types = {name: variable.data_type for name, variable in resolved.variables.items()}
        store = VariableStore(types)
        if previous is not None and (
            previous.config.settings.device_id == resolved.settings.device_id
        ):
            store.seed_from(previous.store)

        async def record_write(variable_id: str, value: Scalar) -> None:
            event_id = uuid4().hex
            self.observability.record(server_id, "client_writes_total")
            self.observability.event(server_id, f"protocol write event {event_id}")
            self.write_events.append(
                ProtocolWriteEvent(
                    event_id=event_id,
                    gateway_id=server_id,
                    variable_id=variable_id,
                    value=value,
                    observed_at=datetime.now(UTC),
                )
            )
            if not resolved.settings.control.enabled:
                return
            forwarder = self.forwarders.get(server_id)
            if forwarder is None:
                self.observability.event(
                    server_id,
                    "control forwarder not ready",
                    severity="ERROR",
                    error_code="FORWARDER_UNAVAILABLE",
                )
                return
            for mapping in resolved.settings.control.mappings:
                if mapping.variable_id == variable_id:
                    forwarder.capture(
                        event_id=event_id,
                        gateway_id=server_id,
                        device_id=resolved.settings.device_id,
                        point_id=mapping.target_point_id,
                        value=value,
                        revision=revision,
                    )
                    break

        protocol_server: DeviceModbusServer | None = None
        if resolved.protocol == "modbus":
            protocol_server = DeviceModbusServer(
                host=resolved.settings.listen_host,
                port=resolved.settings.listen_port,
                unit_id=self._unit_id(resolved),
                mappings=resolved.registers,
                store=store,
                on_write=record_write,
                allowed_writes=frozenset(
                    mapping.variable_id for mapping in resolved.settings.control.mappings
                )
                if resolved.settings.control.enabled
                else frozenset(),
            )
        elif resolved.settings.desired_state == "START":
            raise ValueError(f"{server_id}: adapter {resolved.protocol} is not implemented")
        return GatewayInstance(
            config=resolved,
            lifecycle=ServerLifecycle(DesiredState(resolved.settings.desired_state)),
            store=store,
            protocol_server=protocol_server,
        )

    async def reconfigure(
        self, new_config: ResolvedGateway
    ) -> tuple[list[ApplicationResult], bool]:
        """Apply Server-level differences after disk commit; report partial failure."""
        async with self._configuration_lock:
            restart = any(
                (
                    self._process_service.grpc != new_config.service.grpc,
                    self._process_service.metrics != new_config.service.metrics,
                )
            )
            results: list[ApplicationResult] = []
            all_ids = list(dict.fromkeys([*self.instances, *new_config.servers]))
            changed = [
                server_id
                for server_id in all_ids
                if server_id not in self.instances
                or new_config.servers.get(server_id) != self.instances[server_id].config
            ]
            errors: dict[str, Exception] = {}
            # Stop all affected listeners before starting replacements. This
            # permits two configured servers to exchange their listening ports.
            for server_id in changed:
                if server_id not in self.instances:
                    continue
                try:
                    await self.stop_server(server_id)
                    self.variable_leases.invalidate(server_id)
                except Exception as exc:
                    logger.exception("Gateway old listener could not stop: %s", server_id)
                    errors[server_id] = exc

            for server_id in all_ids:
                previous = self.instances.get(server_id)
                updated = new_config.servers.get(server_id)
                if server_id not in changed:
                    results.append(ApplicationResult(object_id=server_id, status="UNCHANGED"))
                    continue
                if server_id in errors:
                    results.append(
                        ApplicationResult(
                            object_id=server_id,
                            status="FAILED",
                            error_code="RESOURCE_RELEASE_FAILED",
                            message=str(errors[server_id]),
                        )
                    )
                    continue
                try:
                    old_forwarder = self.forwarders.get(server_id)
                    if old_forwarder is not None:
                        await old_forwarder.stop()
                        self.forwarders.pop(server_id, None)
                    if updated is None:
                        self.instances.pop(server_id, None)
                        results.append(ApplicationResult(object_id=server_id, status="REMOVED"))
                        continue
                    replacement = self._make_instance(
                        server_id,
                        updated,
                        new_config.revision,
                        previous,
                    )
                    if updated.settings.control.enabled:
                        forwarder = CommandForwarder(
                            updated.settings.control.target,
                            project_root=new_config.project_root,
                            observer=self._record_forward,
                        )
                        try:
                            await forwarder.start()
                        except BaseException:
                            await forwarder.stop()
                            raise
                        self.forwarders[server_id] = forwarder
                    # Never expose an enabled control listener before its
                    # authenticated outbound forwarder is ready.
                    self.instances[server_id] = replacement
                    if replacement.lifecycle.status.desired is DesiredState.START:
                        await self.start_server(server_id)
                    results.append(ApplicationResult(object_id=server_id, status="APPLIED"))
                except Exception as exc:
                    logger.exception("Gateway new listener failed to apply: %s", server_id)
                    results.append(
                        ApplicationResult(
                            object_id=server_id,
                            status="FAILED",
                            error_code="RUNTIME_APPLY_FAILED",
                            message=str(exc),
                        )
                    )
            if not any(result.status == "FAILED" for result in results):
                self.config = new_config
            return results, restart

    async def start_requested(self, server_id: str, expected_revision: str) -> None:
        """Compare revision under the same lock as configuration application."""
        async with self._configuration_lock:
            if expected_revision != self.config.revision:
                raise ValueError("stale gateway revision")
            await self.start_server(server_id)

    async def stop_requested(self, server_id: str, expected_revision: str) -> None:
        """Compare revision under the same lock as configuration application."""
        async with self._configuration_lock:
            if expected_revision != self.config.revision:
                raise ValueError("stale gateway revision")
            await self.stop_server(server_id)

    async def claim_targets(self, request: ClaimRequest) -> ClaimResult:
        """Serialize target leasing with Server replacement and stop/start."""
        async with self._configuration_lock:
            instance = self.instances[request.gateway_id]
            return await self.variable_leases.claim(
                request,
                frozenset(instance.config.variables),
            )

    async def update_variables(
        self,
        server_id: str,
        values: list[tuple[str, Scalar | None]],
        lease_token: str,
        principal: str,
    ) -> list[UpdateResult]:
        """Fence all target ownership before applying independent item updates."""
        async with self._configuration_lock:
            instance = self.instances[server_id]
            return await self.variable_leases.update(
                server_id, instance.store, values, lease_token, principal
            )

    @staticmethod
    def _unit_id(server: ResolvedServer) -> int:
        if server.unit_id is None:
            raise ValueError("Modbus Server has no effective Unit ID")
        return server.unit_id

    async def start(self) -> None:
        """Start configured listeners and reconcile failed START objects."""
        self._closing = False
        started: list[CommandForwarder] = []
        try:
            for forwarder in self.forwarders.values():
                await forwarder.start()
                started.append(forwarder)
        except BaseException:
            for forwarder in reversed(started):
                await forwarder.stop()
            raise
        self.observability.start(lambda: tuple(self.instances))
        for server_id, instance in self.instances.items():
            if instance.lifecycle.status.desired is DesiredState.START:
                try:
                    await self.start_server(server_id)
                except Exception:
                    logger.exception("Gateway startup failed for %s", server_id)
        self._watchdog = asyncio.create_task(self._reconcile())

    async def _reconcile(self) -> None:
        try:
            while not self._closing:
                await asyncio.sleep(2.0)
                for server_id, instance in tuple(self.instances.items()):
                    if self.instances.get(server_id) is not instance:
                        continue
                    async with instance.lock:
                        if (
                            instance.lifecycle.status.actual is ActualState.RUNNING
                            and instance.protocol_server is not None
                            and not instance.protocol_server.is_serving
                        ):
                            instance.lifecycle.mark_failure(
                                "Protocol listener stopped unexpectedly"
                            )
                        should_recover = instance.lifecycle.can_recover
                    if should_recover and (self.instances.get(server_id) is instance):
                        try:
                            await self.start_server(server_id, recover=True)
                        except Exception:
                            logger.exception("Gateway recovery failed: %s", server_id)
        except asyncio.CancelledError:
            raise

    async def start_server(self, server_id: str, *, recover: bool = False) -> None:
        """Start a single listener, serializing start/stop and enforcing release."""
        instance = self.instances[server_id]
        async with instance.lock:
            if self._closing:
                raise RuntimeError("Gateway runtime is shutting down")
            status = instance.lifecycle.status
            if status.actual in {ActualState.STARTING, ActualState.RUNNING}:
                return
            if instance.protocol_server is None:
                raise RuntimeError("unsupported protocol Server")
            if recover and not instance.lifecycle.can_recover:
                return
            if status.actual is ActualState.FAILED:
                try:
                    await instance.protocol_server.stop()
                except Exception:
                    logger.exception("Cannot safely release failed Server %s", server_id)
                    raise
                if recover:
                    instance.lifecycle.begin_recovery(resources_released=True)
                else:
                    instance.lifecycle.request_start(resources_released=True)
            else:
                instance.lifecycle.request_start()
            try:
                await instance.protocol_server.start()
            except Exception as exc:
                instance.lifecycle.mark_failure(str(exc))
                self.observability.event(
                    server_id,
                    "protocol Server start failed",
                    severity="ERROR",
                    error_code="SERVER_START_FAILED",
                )
                raise
            instance.lifecycle.mark_running()
            self.observability.event(
                server_id,
                (
                    "protocol Server started; "
                    f"restart_count={instance.lifecycle.status.restart_count}; "
                    f"last_failure_at={instance.lifecycle.status.last_failure_at}"
                ),
            )

    async def stop_server(self, server_id: str) -> None:
        """Explicit STOP permanently disables watchdog recovery until START."""
        instance = self.instances[server_id]
        async with instance.lock:
            instance.lifecycle.request_stop()
            if instance.protocol_server is None:
                if instance.lifecycle.status.actual is not ActualState.STOPPED:
                    instance.lifecycle.mark_stopped()
                self.observability.event(server_id, "protocol Server stopped")
                return
            try:
                await instance.protocol_server.stop()
            except Exception as exc:
                instance.lifecycle.mark_failure(str(exc))
                self.observability.event(
                    server_id,
                    "protocol Server shutdown failed",
                    severity="ERROR",
                    error_code="SERVER_STOP_FAILED",
                )
                raise
            if instance.lifecycle.status.actual is not ActualState.STOPPED:
                instance.lifecycle.mark_stopped()
            self.observability.event(server_id, "protocol Server stopped")

    async def stop(self) -> None:
        """Release watchdog and listeners in an ordered shutdown."""
        self._closing = True
        task, self._watchdog = self._watchdog, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        first_error: Exception | None = None
        for server_id in reversed(list(self.instances)):
            try:
                await self.stop_server(server_id)
            except Exception as exc:
                logger.exception("Gateway shutdown failed: %s", server_id)
                if first_error is None:
                    first_error = exc
        try:
            await self.observability.stop()
        except Exception as exc:
            if first_error is None:
                first_error = exc
        for forwarder in self.forwarders.values():
            try:
                await forwarder.stop()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise RuntimeError("one or more Gateway resources could not stop") from (first_error)
