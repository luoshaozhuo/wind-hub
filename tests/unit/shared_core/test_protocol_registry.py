from __future__ import annotations

from collections.abc import Sequence

import pytest

from core.application import (
    ConfigError,
    ConnectionHealth,
    ProtocolCapability,
    ProtocolCapabilityError,
    ProtocolSample,
    ProtocolSampleCallback,
    ProtocolWrite,
    ProtocolWriteResult,
    SubscriptionHandle,
)
from core.domain import (
    UNIT_CATALOG,
    BusinessPoint,
    ConnectionEndpoint,
    DataType,
    Device,
    DeviceModel,
    DeviceType,
    Point,
    PointAccess,
    PointTable,
    Protocol,
    UnitCode,
    point_table_for_device,
    protocol_options_for,
)
from core.infrastructure import ProtocolRegistry
from core.infrastructure.protocol.ads import ADSDriver
from core.infrastructure.protocol.iec104 import IEC104Driver
from core.infrastructure.protocol.modbus import ModbusDriver


class _Protocol:
    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.WRITE,
            }
        )

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=True)

    async def read(
        self,
        point_ids: Sequence[str],
    ) -> tuple[ProtocolSample, ...]:
        del point_ids
        return ()

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        del writes
        return ()

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        del point_ids, callback, interval
        raise ProtocolCapabilityError("not supported")

    async def interrogate(self) -> None:
        raise ProtocolCapabilityError("not supported")


def test_protocol_write_normalizes_point_id() -> None:
    write = ProtocolWrite(point_id=" power ", value=1.0)

    assert write.point_id == "power"

    with pytest.raises(ValueError, match="point_id"):
        ProtocolWrite(point_id=" ", value=1.0)


def test_protocol_registry_is_explicit_and_case_normalized() -> None:
    registry = ProtocolRegistry()
    registry.register(
        "Modbus",
        lambda _endpoint, _point_table, _device_options: _Protocol(),
    )

    endpoint = ConnectionEndpoint("127.0.0.1", 502)
    point_table = PointTable("pt", Protocol("MODBUS"), {})
    protocol = registry.create(endpoint, point_table, {})

    assert registry.registered_names() == ("modbus",)
    assert protocol.health().healthy is True


def test_protocol_registry_accepts_builtin_driver_classes() -> None:
    registry = ProtocolRegistry()
    registry.register("modbus", ModbusDriver)
    registry.register("ads", ADSDriver)
    registry.register("iec104", IEC104Driver)

    assert registry.registered_names() == ("ads", "iec104", "modbus")


def test_protocol_registry_create_fails_fast_on_invalid_driver_config() -> None:
    registry = ProtocolRegistry()
    registry.register("modbus", ModbusDriver)

    device_type = DeviceType("turbine", "Turbine")
    business_point = BusinessPoint(
        "power",
        DataType.FLOAT32,
        UNIT_CATALOG[UnitCode.KILOWATT],
    )
    protocol_point = Point(
        point_id="power",
        business_point_id=business_point.business_point_id,
        source_unit=UNIT_CATALOG[UnitCode.KILOWATT],
        access=PointAccess.READ,
        ext={
            "register_type": "input",
            "data_type": "float32",
            # address intentionally missing
        },
    )
    table = PointTable(
        "pt",
        Protocol("modbus"),
        {"power": protocol_point},
    )
    model = DeviceModel(
        "m1",
        device_type.device_type_id,
        table.point_table_id,
    )
    device = Device(
        "d1",
        model.device_model_id,
        ConnectionEndpoint("127.0.0.1", 502),
    )
    with pytest.raises(ConfigError, match="address"):
        registry.create(
            device.endpoint,
            point_table_for_device(
                {device.device_id: device},
                {model.device_model_id: model},
                {table.point_table_id: table},
                device.device_id,
            ),
            protocol_options_for({}, device.device_id),
        )


def test_protocol_drivers_declare_supported_capabilities() -> None:
    modbus = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable("modbus_pt", Protocol("modbus"), {}),
        {},
    )
    ads = ADSDriver(
        ConnectionEndpoint("127.0.0.1", 801),
        PointTable("ads_pt", Protocol("ads"), {}),
        {},
    )
    iec104 = IEC104Driver(
        ConnectionEndpoint("127.0.0.1", 2404),
        PointTable("iec104_pt", Protocol("iec104"), {}),
        {},
    )

    assert modbus.capabilities() == frozenset(
        {
            ProtocolCapability.READ,
            ProtocolCapability.WRITE,
        }
    )
    assert ads.capabilities() == frozenset(
        {
            ProtocolCapability.READ,
            ProtocolCapability.WRITE,
            ProtocolCapability.SUBSCRIBE,
        }
    )
    assert iec104.capabilities() == frozenset(ProtocolCapability)


@pytest.mark.asyncio
async def test_modbus_rejects_unsupported_protocol_capabilities() -> None:
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable("modbus_pt", Protocol("modbus"), {}),
        {},
    )

    async def callback(_sample: ProtocolSample) -> None:
        return None

    with pytest.raises(ProtocolCapabilityError, match="subscription"):
        await driver.subscribe((), callback)

    with pytest.raises(ProtocolCapabilityError, match="interrogation"):
        await driver.interrogate()


@pytest.mark.asyncio
async def test_ads_rejects_unsupported_interrogation() -> None:
    driver = ADSDriver(
        ConnectionEndpoint("127.0.0.1", 801),
        PointTable("ads_pt", Protocol("ads"), {}),
        {},
    )

    with pytest.raises(ProtocolCapabilityError, match="interrogation"):
        await driver.interrogate()


def test_protocol_registry_is_explicitly_assembled_by_caller() -> None:
    registry = ProtocolRegistry()
    registry.register("ads", ADSDriver)
    registry.register("iec104", IEC104Driver)
    registry.register("modbus", ModbusDriver)

    assert registry.registered_names() == ("ads", "iec104", "modbus")
