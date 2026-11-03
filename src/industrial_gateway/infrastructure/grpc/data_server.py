"""Industrial Gateway public RPC: virtual variable storage and lifecycle."""

from __future__ import annotations

import hashlib
from ipaddress import ip_address
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as gateway
from contracts import industrial_gateway_pb2_grpc as gateway_rpc
from core.infrastructure.rpc.scalar_codec import decode_scalar, encode_scalar
from industrial_gateway.application.point_store import PointStore
from industrial_gateway.domain.point import PointRole
from industrial_gateway.infrastructure.config.loader import GatewayConfig

LifecycleAction = Callable[[str], Awaitable[bool]]
IsRunning = Callable[[str], bool]


def _time_now() -> Timestamp:
    value = Timestamp()
    value.FromDatetime(datetime.now(timezone.utc))
    return value


class IndustrialGatewayService(gateway_rpc.IndustrialGatewayServiceServicer):
    """One externally visible Gateway per configured industrial protocol endpoint."""

    def __init__(
        self,
        store: PointStore,
        config: GatewayConfig,
        start: LifecycleAction,
        stop: LifecycleAction,
        is_running: IsRunning,
    ) -> None:
        self._store = store
        self._config = config
        self._revision = hashlib.sha256(
            config.model_dump_json().encode("utf-8")
        ).hexdigest()
        self._start = start
        self._stop = stop
        self._is_running = is_running
        self._definitions: dict[str, dict[str, tuple[str, str, str, PointRole]]] = {}
        for gateway_id in config.endpoints:
            self._definitions[gateway_id] = {}
        for device_id, device in config.devices.items():
            target = self._definitions[device.endpoint_id]
            point_table = config.point_tables[device.point_table_id]
            for point_id, point in point_table.points.items():
                target[f"{device_id}.{point_id}"] = (
                    device_id, point_id, point.data_type, PointRole(point.role),
                )

    def _catalog(self, gateway_id: str) -> dict[str, tuple[str, str, str, PointRole]]:
        try:
            return self._definitions[gateway_id]
        except KeyError as exc:
            raise ValueError(f"unknown gateway: {gateway_id}") from exc

    async def _require_catalog(
        self, gateway_id: str, context: grpc.aio.ServicerContext,
    ) -> dict[str, tuple[str, str, str, PointRole]]:
        try:
            return self._catalog(gateway_id)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            raise RuntimeError("gRPC abort returned") from exc

    def _gateway(self, gateway_id: str) -> gateway.Gateway:
        members = [name for name, device in self._config.devices.items()
                   if device.endpoint_id == gateway_id]
        return gateway.Gateway(
            gateway_id=gateway_id, endpoint=self._config.data_grpc_bind or "",
            mapping_summary=f"{len(members)} virtual devices",
            revision=self._revision,
            desired_state="RUNNING" if self._is_running(gateway_id) else "STOPPED",
            actual_state="RUNNING" if self._is_running(gateway_id) else "STOPPED",
        )

    async def ListGateways(
        self, request: gateway.ListGatewaysRequest, context: grpc.aio.ServicerContext,
    ) -> gateway.ListGatewaysResponse:
        names = [name for name in sorted(self._definitions) if request.filter in name]
        try:
            offset = int(request.page_token or "0")
            if offset < 0 or offset > len(names):
                raise ValueError("out of range")
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid page token")
            raise RuntimeError("gRPC abort returned") from exc
        size = min(request.page_size or 100, 1000)
        end = offset + size
        return gateway.ListGatewaysResponse(
            gateways=[self._gateway(name) for name in names[offset:end]],
            next_page_token=str(end) if end < len(names) else "",
        )

    async def GetGateway(
        self, request: common.ObjectId, context: grpc.aio.ServicerContext,
    ) -> gateway.GetGatewayResponse:
        await self._require_catalog(request.id, context)
        return gateway.GetGatewayResponse(gateway=self._gateway(request.id))

    async def _lifecycle(
        self, request: common.OperationRequest, context: grpc.aio.ServicerContext,
        action: LifecycleAction, desired: str,
    ) -> common.OperationResult:
        await self._require_catalog(request.id, context)
        if request.expected_revision and request.expected_revision != self._revision:
            return common.OperationResult(
                accepted=False, revision=self._revision,
                desired_state=desired, actual_state="CONFLICT",
                error_code="REVISION_CONFLICT",
            )
        try:
            changed = await action(request.id)
        except (OSError, RuntimeError, ValueError) as exc:
            return common.OperationResult(
                accepted=False, actual_state="FAILED", desired_state=desired,
                error_code="LIFECYCLE_FAILED", message=str(exc),
            )
        return common.OperationResult(
            accepted=True, desired_state=desired, revision=self._revision,
            actual_state="RUNNING" if self._is_running(request.id) else "STOPPED",
            message="changed" if changed else "already in requested state",
        )

    async def StartGateway(
        self, request: common.OperationRequest, context: grpc.aio.ServicerContext,
    ) -> common.OperationResult:
        return await self._lifecycle(request, context, self._start, "RUNNING")

    async def StopGateway(
        self, request: common.OperationRequest, context: grpc.aio.ServicerContext,
    ) -> common.OperationResult:
        return await self._lifecycle(request, context, self._stop, "STOPPED")

    async def ListVariables(
        self, request: gateway.ListVariablesRequest, context: grpc.aio.ServicerContext,
    ) -> gateway.ListVariablesResponse:
        catalog = await self._require_catalog(request.gateway_id, context)
        names = [name for name in sorted(catalog) if request.filter in name]
        try:
            offset = int(request.page_token or "0")
            if offset < 0 or offset > len(names):
                raise ValueError("out of range")
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid page token")
            raise RuntimeError("gRPC abort returned") from exc
        size = min(request.page_size or 100, 1000)
        end = offset + size
        variables = [
            gateway.Variable(
                variable_id=name, name=name, data_type=catalog[name][2],
                readable=True, writable=catalog[name][3] is PointRole.FEEDBACK,
            )
            for name in names[offset:end]
        ]
        return gateway.ListVariablesResponse(
            variables=variables,
            next_page_token=str(end) if end < len(names) else "",
        )

    async def ReadVariables(
        self, request: gateway.ReadVariablesRequest, context: grpc.aio.ServicerContext,
    ) -> gateway.ReadVariablesResponse:
        catalog = await self._require_catalog(request.gateway_id, context)
        names = request.variable_ids or tuple(sorted(catalog))
        samples: list[common.Sample] = []
        for name in names:
            if name not in catalog:
                await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown variable: {name}")
                raise RuntimeError("gRPC abort returned")
            device_id, point_id, _, _ = catalog[name]
            state = self._store.read(device_id, point_id)
            sample = common.Sample(
                point_id=name, quality="GOOD" if state.value is not None else "UNKNOWN",
                freshness="CURRENT" if state.value is not None else "UNAVAILABLE",
                queried_at=_time_now(),
            )
            if state.value is not None:
                sample.value.CopyFrom(encode_scalar(state.value))
            else:
                sample.error_code = "NO_VALUE"
            samples.append(sample)
        return gateway.ReadVariablesResponse(
            gateway_id=request.gateway_id, values=samples,
        )

    async def UpdateVariables(
        self, request: gateway.UpdateVariablesRequest, context: grpc.aio.ServicerContext,
    ) -> gateway.UpdateVariablesResponse:
        # This service is currently loopback-only. Enforce the configured
        # data-writer IP allowlist in addition to the network bind restriction.
        peer = context.peer()
        address = ""
        if peer.startswith("ipv4:"):
            address = peer[5:].rsplit(":", 1)[0]
        elif peer.startswith("ipv6:"):
            address = peer[5:].rsplit(":", 1)[0].strip("[]")
        try:
            authorized = ip_address(address) in {
                ip_address(value)
                for value in self._config.control.access.data_writers
            }
        except ValueError:
            authorized = False
        if not authorized:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "Gateway data writer is not authorized",
            )
            raise RuntimeError("gRPC abort returned")
        catalog = await self._require_catalog(request.gateway_id, context)
        if not 0 < len(request.values) <= 5000:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "batch size must be 1..5000")
            raise RuntimeError("gRPC abort returned")
        seen: set[str] = set()
        results: list[gateway.VariableUpdateResult] = []
        for item in request.values:
            code = ""
            message = ""
            try:
                if item.variable_id in seen:
                    raise ValueError("duplicate target in request")
                seen.add(item.variable_id)
                if item.variable_id not in catalog:
                    raise KeyError("unknown target variable")
                device_id, point_id, _, role = catalog[item.variable_id]
                if role is not PointRole.FEEDBACK:
                    raise PermissionError("cannot modify a command or parameter variable")
                if not item.HasField("value") or item.value.WhichOneof("kind") is None:
                    raise ValueError("missing scalar value")
                value = decode_scalar(item.value)
                await self._store.update_feedback(device_id, point_id, value)
            except (ValueError, KeyError, TypeError, PermissionError) as exc:
                code = "INVALID_VARIABLE"
                message = str(exc)
            results.append(gateway.VariableUpdateResult(
                variable_id=item.variable_id, success=not code,
                error_code=code, message=message,
            ))
        return gateway.UpdateVariablesResponse(results=results)


def create_grpc_server(
    store: PointStore,
    bind: str,
    config: GatewayConfig,
    start: LifecycleAction,
    stop: LifecycleAction,
    is_running: IsRunning,
) -> grpc.aio.Server:
    if not bind.startswith(("127.0.0.1:", "[::1]:")):
        raise ValueError("unauthenticated Gateway gRPC must bind loopback")
    server = grpc.aio.server()
    gateway_rpc.add_IndustrialGatewayServiceServicer_to_server(
        IndustrialGatewayService(store, config, start, stop, is_running), server,
    )
    if server.add_insecure_port(bind) == 0:
        raise RuntimeError("unable to bind Gateway gRPC listener")
    return server
