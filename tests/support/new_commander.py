"""新 Commander（src/commander）测试共享装配辅助。

提供最小 YAML 配置树写出、Fake ProtocolPort 与内存 CommanderConfig 构造，
供 unit/component 层复用；不 import 任何 wind_hub_* 模块。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from commander.application.config import CommanderConfig, PointMeta
from core.application import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolPort,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
)
from core.application.port.protocol import SubscriptionHandle
from core.domain import (
    BusinessPoint,
    BusinessPointId,
    ConnectionEndpoint,
    CoreConfigSnapshot,
    DataType,
    Device,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    DeviceType,
    DeviceTypeId,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
    ProtocolOptions,
)
from core.domain.unit import UNIT_CATALOG, UnitCode

# ---------------------------------------------------------------------------
# YAML 配置树
# ---------------------------------------------------------------------------


def write_yaml(dir_path: Path, name: str, data: dict[str, Any]) -> None:
    """写出一个 YAML 文件。"""
    with open(dir_path / name, "w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)


def write_minimal_config_tree(
    base: Path,
    *,
    protocol: str = "modbus",
    address: dict[str, Any] | None = None,
    devices: list[dict[str, Any]] | None = None,
    points: list[dict[str, Any]] | None = None,
    point_tables: dict[str, Any] | None = None,
    connection_defaults: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    ads: dict[str, Any] | None = None,
    read_mode: str | None = None,
) -> Path:
    """写出最小自包含配置树（单型号 ``mod``、单点表 ``tab``），返回目录。"""
    base.mkdir(parents=True, exist_ok=True)
    write_yaml(base, "units.yaml", {"units": {"none": {"symbol": "", "name": "None"}}})
    system: dict[str, Any] = {"site": {"site_id": "test"}, "runtime": runtime or {}}
    if ads is not None:
        system["ads"] = ads
    write_yaml(base, "system.yaml", system)

    model: dict[str, Any] = {
        "device_type": "turbine",
        "protocol": protocol,
        "point_table": "tab",
        "connection_defaults": connection_defaults or {},
    }
    if read_mode is not None:
        model["read_mode"] = read_mode
    write_yaml(
        base,
        "device_models.yaml",
        {"device_types": {"turbine": {"name": "风机"}}, "device_models": {"mod": model}},
    )

    if devices is None:
        devices = [
            {
                "device_id": "dev1",
                "model": "mod",
                "endpoint": {"host": "127.0.0.1", "port": 502},
            }
        ]
    write_yaml(base, "devices.yaml", {"devices": devices})

    if point_tables is None:
        if points is None:
            points = [
                {
                    "point_id": "p1",
                    "point_groups": ["g"],
                    "address": address or {"register_type": "holding", "address": 100},
                    "data_type": "float32",
                }
            ]
        point_tables = {"tab": {"protocol": protocol, "points": points}}
    write_yaml(base, "points.yaml", {"point_tables": point_tables})
    return base


# ---------------------------------------------------------------------------
# Fake ProtocolPort
# ---------------------------------------------------------------------------


class FakeSubscriptionHandle:
    """记录式订阅句柄。"""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeProtocol:
    """内存 ProtocolPort：记录 connect/close 次数，读写返回脚本化结果。"""

    def __init__(self) -> None:
        self.connect_calls = 0
        self.close_calls = 0
        self.connected = False
        self.fail_connect = False
        self.read_values: dict[str, Any] = {}
        self.writes: list[ProtocolWrite] = []
        self.write_success = True
        self.capabilities_value: frozenset[ProtocolCapability] = frozenset(
            {ProtocolCapability.READ, ProtocolCapability.WRITE}
        )

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return self.capabilities_value

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.fail_connect:
            raise ConnectionError("fake connect failure")
        self.connected = True

    async def close(self) -> None:
        self.close_calls += 1
        self.connected = False

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=self.connected)

    async def read(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        return tuple(
            ProtocolSample(
                point_id=point_id,
                value=self.read_values.get(point_id, 1.0),
                quality=Quality.GOOD,
            )
            for point_id in point_ids
        )

    async def write(self, writes: Sequence[ProtocolWrite]) -> tuple[ProtocolWriteResult, ...]:
        self.writes.extend(writes)
        return tuple(
            ProtocolWriteResult(
                point_id=write.point_id,
                success=self.write_success,
                message=None if self.write_success else "fake write rejected",
            )
            for write in writes
        )

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: Any,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        return FakeSubscriptionHandle()

    async def interrogate(self) -> None:
        return None


class FakeRegistry:
    """记录每次 create 的协议注册表替身（与 ProtocolRegistry 同形）。"""

    def __init__(self) -> None:
        self.instances: list[FakeProtocol] = []

    def create(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        device_options: ProtocolOptions,
    ) -> ProtocolPort:
        instance = FakeProtocol()
        self.instances.append(instance)
        return instance


# ---------------------------------------------------------------------------
# 内存 CommanderConfig
# ---------------------------------------------------------------------------


def make_commander_config(
    *,
    device_id: str = "dev1",
    scale: float = 1.0,
    offset: float = 0.0,
    access: PointAccess = PointAccess.READ_WRITE,
    data_type: DataType = DataType.FLOAT32,
    connect_timeout: float = 1.0,
    write_timeout: float = 1.0,
) -> CommanderConfig:
    """构造单设备单点的内存 CommanderConfig（不经过 YAML）。"""
    unit = UNIT_CATALOG[UnitCode.NONE]
    bp = BusinessPoint(
        business_point_id=BusinessPointId("p1"),
        data_type=data_type,
        standard_unit=unit,
    )
    point = Point(
        point_id="p1",
        business_point_id=bp.business_point_id,
        source_unit=unit,
        access=access,
        scale=scale,
        offset=offset,
        ext={"register_type": "holding", "address": 100},
    )
    table = PointTable(
        point_table_id=PointTableId("tab"),
        protocol=Protocol("modbus"),
        points={"p1": point},
    )
    snapshot = CoreConfigSnapshot(
        device_types={
            DeviceTypeId("turbine"): DeviceType(device_type_id=DeviceTypeId("turbine"), name="风机")
        },
        device_models={
            DeviceModelId("mod"): DeviceModel(
                device_model_id=DeviceModelId("mod"),
                device_type_id=DeviceTypeId("turbine"),
                point_table_id=PointTableId("tab"),
            )
        },
        devices={
            DeviceId(device_id): Device(
                device_id=DeviceId(device_id),
                device_model_id=DeviceModelId("mod"),
                endpoint=ConnectionEndpoint(host="127.0.0.1", port=502),
            )
        },
        business_points={bp.business_point_id: bp},
        point_tables={PointTableId("tab"): table},
    )
    return CommanderConfig(
        core=snapshot,
        connect_timeout=connect_timeout,
        write_timeout=write_timeout,
        point_meta={
            PointTableId("tab"): {"p1": PointMeta(variable_name="P1", point_groups=("g",))}
        },
    )


__all__ = [
    "FakeProtocol",
    "FakeRegistry",
    "make_commander_config",
    "write_minimal_config_tree",
    "write_yaml",
]
