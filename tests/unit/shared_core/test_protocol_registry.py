from __future__ import annotations

from collections.abc import Sequence

from core.application import (
    ConnectionEndpoint,
    ConnectionHealth,
    DeviceConnection,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
)
from core.domain import PointTable, Protocol, ProtocolPoint
from core.infrastructure import (
    ADSDriver,
    IEC104Driver,
    ModbusDriver,
    ProtocolRegistry,
)


class _Protocol:
    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=True)

    async def read(
        self,
        points: Sequence[ProtocolPoint],
    ) -> tuple[ProtocolSample, ...]:
        return ()

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        return ()


def test_protocol_registry_is_explicit_and_case_normalized() -> None:
    registry = ProtocolRegistry()
    registry.register("Modbus", lambda _connection, _point_table: _Protocol())

    connection = DeviceConnection(
        "c1",
        "d1",
        ConnectionEndpoint("127.0.0.1", 502),
    )
    point_table = PointTable("pt", Protocol("MODBUS"), {})
    protocol = registry.create(connection, point_table)

    assert registry.registered_names() == ("modbus",)
    assert protocol.health().healthy is True



def test_protocol_registry_accepts_builtin_driver_classes() -> None:
    registry = ProtocolRegistry()
    registry.register("modbus", ModbusDriver)
    registry.register("ads", ADSDriver)
    registry.register("iec104", IEC104Driver)

    assert registry.registered_names() == ("ads", "iec104", "modbus")
