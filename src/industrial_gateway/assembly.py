"""Industrial Gateway V2 组装：多 Unit、一个 Endpoint 一个 pymodbus Server。

当前协议请求来源无法在 SimDevice.action 中认证；运行时默认只读，
明确禁止在未经验证的入口接收数据更新或控制指令。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from industrial_gateway.application.write_service import WriteService
from industrial_gateway.domain import PointDefinition, PointRole
from industrial_gateway.infrastructure.config.loader import GatewayConfig
from industrial_gateway.infrastructure.protocol.modbus.server import PymodbusGatewayServer
from industrial_gateway.modbus_codec import RegisterMapping
from industrial_gateway.point_store import PointStore


@dataclass(slots=True)
class GatewayApplication:
    store: PointStore
    servers: list[PymodbusGatewayServer]

    async def start(self) -> None:
        started: list[PymodbusGatewayServer] = []
        try:
            for server in self.servers:
                await server.start()
                started.append(server)
        except BaseException:
            for server in reversed(started):
                await server.stop()
            raise

    async def stop(self) -> None:
        for server in reversed(self.servers):
            await server.stop()


def assemble_gateway(config: GatewayConfig) -> GatewayApplication:
    definitions: list[PointDefinition] = []
    roles: dict[tuple[str, str], PointRole] = {}
    by_endpoint: dict[str, dict[int, list[RegisterMapping]]] = defaultdict(dict)
    for device_id, device in config.devices.items():
        table = config.point_tables[device.point_table_id]
        mappings: list[RegisterMapping] = []
        for point_id, point in table.points.items():
            role = PointRole(point.role)
            definitions.append(
                PointDefinition(
                    device_id, point_id, role,
                    writable=role in {PointRole.FEEDBACK, PointRole.PARAMETER, PointRole.COMMAND},
                )
            )
            roles[(device_id, point_id)] = role
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

    store = PointStore(definitions)
    writer = WriteService(store, roles)
    servers = [
        PymodbusGatewayServer(
            host=endpoint.host,
            port=endpoint.port,
            units=by_endpoint[endpoint_id],
            store=store,
            writes=writer,
            writer_role=None,
        )
        for endpoint_id, endpoint in config.endpoints.items()
    ]
    return GatewayApplication(store=store, servers=servers)
