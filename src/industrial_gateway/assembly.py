"""Industrial Gateway V2 组装：多 Unit、一个 Endpoint 一个 pymodbus Server。

当前协议请求来源无法在 SimDevice.action 中认证；运行时默认只读，
明确禁止在未经验证的入口接收数据更新或控制指令。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from ipaddress import ip_address

import grpc

from industrial_gateway.application.command_service import CommanderProxy, CommandTarget
from industrial_gateway.application.point_store import PointStore
from industrial_gateway.application.write_service import WriteService
from industrial_gateway.domain import PointDefinition, PointRole
from industrial_gateway.infrastructure.commander.journal import CommandJournal
from industrial_gateway.infrastructure.config.loader import GatewayConfig
from industrial_gateway.infrastructure.grpc.commander import GrpcCommanderWriter
from industrial_gateway.infrastructure.grpc.data_server import create_grpc_server
from industrial_gateway.infrastructure.protocol.modbus.codec import RegisterMapping
from industrial_gateway.infrastructure.protocol.modbus.server import PymodbusGatewayServer


@dataclass(slots=True)
class GatewayApplication:
    store: PointStore
    servers: list[PymodbusGatewayServer]
    commander_channel: grpc.aio.Channel | None = None
    data_server: grpc.aio.Server | None = None
    data_grpc_bind: str | None = None
    config: GatewayConfig | None = None
    endpoint_ids: tuple[str, ...] = ()
    _active: set[str] = field(default_factory=set)
    _operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_running(self, gateway_id: str) -> bool:
        return gateway_id in self._active

    async def start_gateway(self, gateway_id: str) -> bool:
        async with self._operation_lock:
            if gateway_id in self._active:
                return False
            try:
                index = self.endpoint_ids.index(gateway_id)
            except ValueError as exc:
                raise ValueError(f"unknown Gateway: {gateway_id}") from exc
            await self.servers[index].start()
            self._active.add(gateway_id)
            return True

    async def stop_gateway(self, gateway_id: str) -> bool:
        async with self._operation_lock:
            if gateway_id not in self._active:
                return False
            index = self.endpoint_ids.index(gateway_id)
            await self.servers[index].stop()
            self._active.remove(gateway_id)
            return True

    async def start(self) -> None:
        try:
            if self.data_grpc_bind is not None:
                if self.config is None:
                    raise RuntimeError("Gateway config missing for gRPC service")
                self.data_server = create_grpc_server(
                    self.store,
                    self.data_grpc_bind,
                    self.config,
                    self.start_gateway,
                    self.stop_gateway,
                    self.is_running,
                )
                await self.data_server.start()
            for gateway_id in self.endpoint_ids:
                await self.start_gateway(gateway_id)
        except BaseException:
            for gateway_id in reversed(self.endpoint_ids):
                if gateway_id in self._active:
                    await self.stop_gateway(gateway_id)
            if self.data_server is not None:
                await self.data_server.stop(grace=0)
            raise

    async def stop(self) -> None:
        if self.data_server is not None:
            await self.data_server.stop(grace=0)
        for gateway_id in reversed(self.endpoint_ids):
            if gateway_id in self._active:
                await self.stop_gateway(gateway_id)
        if self.commander_channel is not None:
            await self.commander_channel.close()


def assemble_gateway(config: GatewayConfig) -> GatewayApplication:
    definitions: list[PointDefinition] = []
    roles: dict[tuple[str, str], PointRole] = {}
    max_age_seconds: dict[tuple[str, str], float] = {}
    by_endpoint: dict[str, dict[int, list[RegisterMapping]]] = defaultdict(dict)
    for device_id, device in config.devices.items():
        table = config.point_tables[device.point_table_id]
        mappings: list[RegisterMapping] = []
        for point_id, point in table.points.items():
            role = PointRole(point.role)
            definitions.append(
                PointDefinition(
                    device_id,
                    point_id,
                    role,
                    writable=point.address.register_type in {"coil", "holding"},
                    data_type=point.data_type,
                )
            )
            roles[(device_id, point_id)] = role
            if role is PointRole.FEEDBACK and point.max_age_seconds is not None:
                max_age_seconds[(device_id, point_id)] = point.max_age_seconds
            mappings.append(
                RegisterMapping(
                    device_id,
                    point_id,
                    point.address.register_type,
                    point.address.address,
                    point.data_type,
                    point.address.word_order,
                )
            )
        by_endpoint[device.endpoint_id][device.protocol_address.unit_id] = mappings

    commander_channel: grpc.aio.Channel | None = None
    command_handler = None
    if config.control.enabled:
        commander_config = config.control.commander
        commander_channel = grpc.aio.insecure_channel(commander_config.endpoint)
        stub = GrpcCommanderWriter(commander_channel)
        targets = {
            (device_id, point_id): CommandTarget(
                target.device_id,
                target.point_id,
                commander_config.command_timeout,
                commander_config.rpc_timeout,
            )
            for device_id, device in config.devices.items()
            for point_id, target in device.commands.items()
        }
        command_handler = CommanderProxy(stub, targets, CommandJournal(config.control.journal_path))
    store = PointStore(
        definitions,
        command_handler=command_handler,
        max_age_seconds=max_age_seconds,
    )
    writer = WriteService(store, roles)
    servers = [
        PymodbusGatewayServer(
            host=endpoint.host,
            port=endpoint.port,
            units=by_endpoint[endpoint_id],
            store=store,
            writes=writer,
            data_writers=frozenset(
                str(ip_address(ip)) for ip in config.control.access.data_writers
            ),
            command_writers=(
                frozenset(str(ip_address(ip)) for ip in config.control.access.command_writers)
                if config.control.enabled
                else frozenset()
            ),
        )
        for endpoint_id, endpoint in config.endpoints.items()
    ]
    return GatewayApplication(
        store=store,
        servers=servers,
        commander_channel=commander_channel,
        data_grpc_bind=config.data_grpc_bind,
        config=config,
        endpoint_ids=tuple(config.endpoints),
    )
