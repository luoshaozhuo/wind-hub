"""新 Commander（src/commander）测试共享装配辅助。

提供最小 YAML 配置树写出、Fake ProtocolPort 与内存 CommanderConfig 构造，
供 unit/component 层复用；不 import 任何 wind_hub_* 模块。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from commander.application.config import CommanderConfig
from core.application import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolPort,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    WriteOutcome,
)
from core.application.errors import ProtocolCapabilityError
from core.application.port.protocol import ListenHandle, SubscriptionHandle
from core.application.protocol_registry import ProtocolRegistry
from core.application.settings import ADSLocalConfig
from core.domain import (
    BusinessPoint,
    BusinessPointId,
    ConnectionEndpoint,
    DataType,
    Device,
    DeviceGroupId,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    DeviceTypeId,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
    ProtocolOptions,
)
from tests.support.units import MINIMAL_UNITS_DOCUMENT, NONE_UNIT, minimal_unit_catalog

# ---------------------------------------------------------------------------
# YAML 配置树
# ---------------------------------------------------------------------------

_ADS_LOGICAL_TYPES = {
    "BOOL": "bool", "SINT": "int8", "USINT": "uint8",
    "INT": "int16", "UINT": "uint16", "DINT": "int32",
    "UDINT": "uint32", "LINT": "int64", "ULINT": "uint64",
    "REAL": "float32", "LREAL": "float64", "STRING": "str",
}


def write_yaml(dir_path: Path, name: str, data: dict[str, Any]) -> None:
    """写出一个 YAML 文件。"""
    with open(dir_path / name, "w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)


def write_minimal_config_tree(
    base: Path,
    *,
    protocol: str = "modbus",
    address: dict[str, Any] | None = None,
    devices: dict[str, Any] | None = None,
    points: dict[str, Any] | None = None,
    point_tables: dict[str, Any] | None = None,
    endpoint_defaults: dict[str, Any] | None = None,
    protocol_options: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    ads: dict[str, Any] | None = None,
) -> Path:
    """写出最小自包含配置树（单型号 ``mod``、单点表 ``tab``），返回目录。

    全部实体集合使用统一 Schema 的 Mapping 形式（Key 即实体 ID）。
    """
    base.mkdir(parents=True, exist_ok=True)
    system: dict[str, Any] = {"site": {"site_id": "test"}, "runtime": runtime or {}}
    if ads is not None:
        system["ads"] = ads
    write_yaml(base, "system.yaml", system)

    model: dict[str, Any] = {
        "device_type_id": "turbine",
        "protocol": protocol,
        "point_table_id": "tab",
    }
    if endpoint_defaults:
        model["endpoint_defaults"] = endpoint_defaults
    if protocol_options:
        model["protocol_options"] = protocol_options
    write_yaml(
        base,
        "device_models.yaml",
        {"device_types": {"turbine": {"name": "风机"}}, "device_models": {"mod": model}},
    )

    if devices is None:
        devices = {
            "dev1": {
                "model_id": "mod",
                "endpoint": {"host": "127.0.0.1", "port": 502},
            }
        }
    instances = {
        device_id: (
            {**entry, "device_group_ids": [device_id]}
            if "device_group_ids" not in entry
            else dict(entry)
        )
        for device_id, entry in devices.items()
    }
    device_groups: dict[str, Any] = {}
    for entry in instances.values():
        for group_id in entry["device_group_ids"]:
            device_groups.setdefault(group_id, {"name": str(group_id)})
    write_yaml(
        base,
        "devices.yaml",
        {"devices": instances, "device_groups": device_groups},
    )

    if point_tables is None:
        if points is None:
            points = {
                "p1": {
                    "point_groups": ["g"],
                    "address": address or {"register_type": "holding", "address": 100},
                    "data_type": "float32",
                }
            }
        point_tables = {"tab": {"protocol": protocol, "points": points}}
    business_points: dict[str, dict[str, str]] = {}
    for table in point_tables.values():
        table_protocol = table.get("protocol", protocol)
        for point_id, point in table.get("points", {}).items():
            if "data_type" in point:
                data_type = point["data_type"]
                if table_protocol == "ads":
                    data_type = _ADS_LOGICAL_TYPES.get(str(data_type).upper(), data_type)
                definition = {
                    "data_type": data_type,
                    "unit": point.get("unit", "none"),
                }
                # 同一 Point Key 跨点表的类型冲突由 loader 检测，helper
                # 保留首个定义即可。
                business_points.setdefault(point_id, definition)
    write_yaml(base, "units.yaml", dict(MINIMAL_UNITS_DOCUMENT))
    write_yaml(base, "business_points.yaml", {"business_points": business_points})
    write_yaml(base, "points.yaml", {"point_tables": point_tables})
    write_yaml(base, "tasks.yaml", {"tasks": {}})
    write_yaml(base, "sinks.yaml", {"sinks": {}})
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
        self.point_table_updates: list[PointTable] = []
        self.interrogate_calls: list[int | None] = []
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

    def is_open(self) -> bool:
        return self.connected

    async def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=self.connected)

    async def read_one(self, point_id: str) -> ProtocolSample:
        return ProtocolSample(
            point_id=point_id,
            value=self.read_values.get(point_id, 1.0),
            quality=Quality.GOOD,
            timestamp=datetime.now(UTC),
        )

    async def read_many(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        return tuple(
            ProtocolSample(
                point_id=point_id,
                value=self.read_values.get(point_id, 1.0),
                quality=Quality.GOOD,
                timestamp=datetime.now(UTC),
            )
            for point_id in point_ids
        )

    async def write_many(self, writes: Sequence[ProtocolWrite]) -> tuple[ProtocolWriteResult, ...]:
        self.writes.extend(writes)
        return tuple(self._write_result(write) for write in writes)

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        self.writes.append(write)
        return self._write_result(write)

    def _write_result(self, write: ProtocolWrite) -> ProtocolWriteResult:
        return ProtocolWriteResult(
            point_id=write.point_id,
            outcome=(
                WriteOutcome.CONFIRMED_SUCCESS
                if self.write_success
                else WriteOutcome.CONFIRMED_FAILURE
            ),
            message=None if self.write_success else "fake write rejected",
        )

    def update_point_table(self, point_table: PointTable) -> None:
        self.point_table_updates.append(point_table)

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: Any,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        # 默认 Fake 只具备 READ/WRITE：与 Modbus 行为一致，明确拒绝。
        raise ProtocolCapabilityError("fake protocol does not support subscribe")

    async def listen(
        self,
        point_ids: Sequence[str],
        callback: Any,
        *,
        interval: float | None = None,
    ) -> ListenHandle:
        raise ProtocolCapabilityError("fake protocol does not support listen")

    async def interrogate(self, common_address: int | None = None) -> None:
        self.interrogate_calls.append(common_address)

    @property
    def interrogate_last(self) -> int | None:
        """最近一次总召目标 CA（None 表示全部站址）。"""
        return self.interrogate_calls[-1] if self.interrogate_calls else None


class FakeRegistry(ProtocolRegistry):
    """记录每次 create 的协议注册表替身（ProtocolRegistry 子类）。"""

    def __init__(self) -> None:
        super().__init__()
        self.instances: list[FakeProtocol] = []

    def create(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
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
    protocol: str = "modbus",
    address: dict[str, Any] | None = None,
    protocol_options_by_device: dict[str, Any] | None = None,
    ads_local: ADSLocalConfig | None = None,
    scale: float = 1.0,
    offset: float = 0.0,
    access: PointAccess = PointAccess.READ_WRITE,
    data_type: DataType = DataType.FLOAT32,
    connect_timeout: float = 1.0,
    write_timeout: float = 1.0,
) -> CommanderConfig:
    """构造单设备单点的内存 CommanderConfig（不经过 YAML）。"""
    unit = NONE_UNIT
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
        ext=address or {"register_type": "holding", "address": 100},
        point_groups=("g",),
    )
    table = PointTable(
        point_table_id=PointTableId("tab"),
        protocol=Protocol(protocol),
        points={"p1": point},
    )
    return CommanderConfig(
        units=minimal_unit_catalog(),
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
                device_group_ids=(DeviceGroupId("grp"),),
                protocol_options=protocol_options_by_device or {},
            )
        },
        business_points={bp.business_point_id: bp},
        point_tables={PointTableId("tab"): table},
        ads_local=ads_local,
        connect_timeout=connect_timeout,
        write_timeout=write_timeout,
    )


__all__ = [
    "FakeProtocol",
    "FakeRegistry",
    "make_commander_config",
    "write_minimal_config_tree",
    "write_yaml",
]
