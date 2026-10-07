from __future__ import annotations

from collections.abc import Sequence

import pytest

from core.application import (
    ConnectionEndpoint,
    ConnectionHealth,
    CoreConfigSnapshot,
    DeviceConnection,
    DeviceSession,
    PointWrite,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
)
from core.domain import (
    BusinessPoint,
    Device,
    DeviceModel,
    DeviceType,
    PointAccess,
    PointTable,
    Protocol,
    ProtocolPoint,
    RawDataType,
    UNIT_CATALOG,
    UnitCode,
    ValueType,
)


class _FakeProtocol:
    def __init__(self) -> None:
        self.writes: tuple[ProtocolWrite, ...] = ()

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
        return tuple(
            ProtocolSample(point_id=point.point_id, value=495.0)
            for point in points
        )

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        self.writes = tuple(writes)
        return tuple(
            ProtocolWriteResult(point_id=write.point.point_id, success=True)
            for write in writes
        )


def _session() -> tuple[DeviceSession, _FakeProtocol]:
    device_type = DeviceType("wind_turbine", "Wind Turbine")
    business_point = BusinessPoint(
        "active_power",
        ValueType.FLOAT,
        UNIT_CATALOG[UnitCode.KILOWATT],
    )
    protocol_point = ProtocolPoint(
        point_id="power",
        business_point_id=business_point.business_point_id,
        raw_type=RawDataType("float32"),
        source_unit=UNIT_CATALOG[UnitCode.WATT],
        access=PointAccess.READ_WRITE,
        scale=2.0,
        offset=10.0,
    )
    table = PointTable(
        "wt_modbus",
        Protocol("modbus"),
        {protocol_point.point_id: protocol_point},
    )
    model = DeviceModel(
        "m1",
        device_type.device_type_id,
        table.point_table_id,
    )
    device = Device("wt01", model.device_model_id)
    connection = DeviceConnection(
        "wt01-main",
        device.device_id,
        ConnectionEndpoint("10.0.0.1", 502),
    )
    snapshot = CoreConfigSnapshot(
        device_types={device_type.device_type_id: device_type},
        device_models={model.device_model_id: model},
        devices={device.device_id: device},
        business_points={business_point.business_point_id: business_point},
        point_tables={table.point_table_id: table},
        device_connections={connection.connection_id: connection},
    )
    protocol = _FakeProtocol()
    return DeviceSession(snapshot, connection, protocol), protocol


@pytest.mark.asyncio
async def test_session_read_normalizes_to_business_unit() -> None:
    session, _ = _session()
    assert session.health().healthy is True

    values = await session.read(("power",))

    assert len(values) == 1
    assert values[0].value == pytest.approx(1.0)
    assert values[0].unit == UNIT_CATALOG[UnitCode.KILOWATT]


@pytest.mark.asyncio
async def test_session_write_reverses_unit_scale_and_offset() -> None:
    session, protocol = _session()

    results = await session.write((PointWrite("power", 1.0),))

    assert results[0].success is True
    assert protocol.writes[0].value == pytest.approx(495.0)



@pytest.mark.asyncio
async def test_session_rejects_duplicate_read_points() -> None:
    session, _ = _session()

    with pytest.raises(ValueError, match="duplicates"):
        await session.read(("power", "power"))


@pytest.mark.asyncio
async def test_session_rejects_duplicate_write_points() -> None:
    session, _ = _session()

    with pytest.raises(ValueError, match="duplicates"):
        await session.write(
            (
                PointWrite("power", 1.0),
                PointWrite("power", 2.0),
            )
        )
