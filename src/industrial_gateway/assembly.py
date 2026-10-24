"""Industrial Gateway V2 组装：多 Unit、一个 Endpoint 一个 pymodbus Server。

当前协议请求来源无法在 SimDevice.action 中认证；运行时默认只读，
明确禁止在未经验证的入口接收数据更新或控制指令。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
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

    async def start(self) -> None:
        started: list[PymodbusGatewayServer] = []
        try:
            if self.data_grpc_bind is not None:
                self.data_server = create_grpc_server(self.store, self.data_grpc_bind)
                await self.data_server.start()
            for server in self.servers:
                await server.start()
                started.append(server)
        except BaseException:
            for server in reversed(started):
                await server.stop()
            if self.data_server is not None:
                await self.data_server.stop(grace=0)
            raise

    async def stop(self) -> None:
        if self.data_server is not None:
            await self.data_server.stop(grace=0)
        for server in reversed(self.servers):
            await server.stop()
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
    )
