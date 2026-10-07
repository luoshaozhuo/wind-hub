from __future__ import annotations

from collections.abc import Sequence

import pytest

from core.application import (
    ConfigError,
    ConnectionHealth,
    CoreConfigSnapshot,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
)
from core.domain import (
    PointAccess,
    PointTable,
    Protocol,
    PointDefinition,
    RawDataType,
    BusinessPoint,
    ConnectionEndpoint,
    Device,
    DeviceModel,
    DeviceType,
    UNIT_CATALOG,
    UnitCode,
    ValueType,
)
from core.infrastructure import (
    ProtocolConfigValidator,
    ProtocolRegistry,
    build_protocol_registry,
)
from core.infrastructure.protocol.ads import ADSDriver
from core.infrastructure.protocol.iec104 import IEC104Driver
from core.infrastructure.protocol.modbus import ModbusDriver


class _Protocol:
    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=True)

    async def read(
        self,
        points: Sequence[PointDefinition],
    ) -> tuple[ProtocolSample, ...]:
        return ()

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        return ()


def test_protocol_registry_is_explicit_and_case_normalized() -> None:
    registry = ProtocolRegistry()
    registry.register(
        "Modbus",
        lambda _endpoint, _point_table, _device_options, _point_options: _Protocol(),
    )

    endpoint = ConnectionEndpoint("127.0.0.1", 502)
    point_table = PointTable("pt", Protocol("MODBUS"), {})
    protocol = registry.create(endpoint, point_table, {}, {})

    assert registry.registered_names() == ("modbus",)
    assert protocol.health().healthy is True



def test_protocol_registry_accepts_builtin_driver_classes() -> None:
    registry = ProtocolRegistry()
    registry.register("modbus", ModbusDriver)
    registry.register("ads", ADSDriver)
    registry.register("iec104", IEC104Driver)

    assert registry.registered_names() == ("ads", "iec104", "modbus")



def test_protocol_config_validator_fails_before_runtime_io() -> None:
    registry = ProtocolRegistry()
    registry.register("modbus", ModbusDriver)
    validator = ProtocolConfigValidator(registry)

    device_type = DeviceType("turbine", "Turbine")
    business_point = BusinessPoint(
        "power",
        ValueType.FLOAT,
        UNIT_CATALOG[UnitCode.KILOWATT],
    )
    protocol_point = PointDefinition(
        point_id="power",
        business_point_id=business_point.business_point_id,
        raw_type=RawDataType("float32"),
        source_unit=UNIT_CATALOG[UnitCode.KILOWATT],
        access=PointAccess.READ,
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
    snapshot = CoreConfigSnapshot(
        device_types={device_type.device_type_id: device_type},
        device_models={model.device_model_id: model},
        devices={device.device_id: device},
        business_points={
            business_point.business_point_id: business_point,
        },
        point_tables={table.point_table_id: table},
        point_options={
            table.point_table_id: {
                protocol_point.point_id: {
                    "register_type": "input",
                    # address intentionally missing
                },
            },
        },
    )

    with pytest.raises(ConfigError, match="address"):
        validator.validate(snapshot)



def test_builtin_protocol_registry_has_all_shared_drivers() -> None:
    registry = build_protocol_registry()

    assert registry.registered_names() == ("ads", "iec104", "modbus")
