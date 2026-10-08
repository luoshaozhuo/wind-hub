"""Commander 现场配置 Raw Schema（YAML 解析目标）。

本包解析与旧系统**同一套**现场配置目录，但只读取 Commander 子集：
``system.yaml``（ads / runtime 连接写超时）、``device_models.yaml``、
``devices.yaml``、``points.yaml``、``units.yaml``。不读取 tasks / sinks。

模型与旧 wind_hub_core 的 Raw Schema 保持一致（字段名、校验规则、错误
语义），但只依赖 core，不 import 任何 wind_hub_* 模块。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.application import ConfigError

SUPPORTED_PROTOCOLS = frozenset({"ads", "modbus", "iec104"})

ALLOWED_DATA_TYPES = frozenset(
    {
        "float32",
        "float64",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "bool",
        "str",
    }
)

ADS_READ_MODES = frozenset({"sum", "sequential"})


def validate_ams_net_id(net_id: str) -> str:
    """校验六段十进制 AMS Net ID，非法时抛 ConfigError。"""
    parts = net_id.split(".")
    if len(parts) != 6 or not all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
        raise ConfigError(f"Invalid AMS Net ID '{net_id}'; expected dotted-numeric 'a.b.c.d.e.f'")
    return net_id


# ---------------------------------------------------------------------------
# system.yaml（Commander 子集）
# ---------------------------------------------------------------------------


class ADSSystemRaw(BaseModel):
    """进程级 ADS 本机身份与凭据。"""

    model_config = ConfigDict(extra="forbid")

    local_ams_net_id: str
    local_ip: str
    username: str = "Administrator"
    password: str = ""

    @field_validator("local_ams_net_id")
    @classmethod
    def _check_local_ams_net_id(cls, value: str) -> str:
        return validate_ams_net_id(value)


# ---------------------------------------------------------------------------
# device_models.yaml
# ---------------------------------------------------------------------------


class DeviceTypeRaw(BaseModel):
    """设备业务类型。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None


class DeviceModelRaw(BaseModel):
    """设备型号——点表绑定在型号层，同型号全部实例共享。"""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    device_type: str
    manufacturer: str | None = None
    model: str | None = None
    protocol: str
    point_table: str
    read_mode: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    connection_defaults: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_model(self) -> DeviceModelRaw:
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ConfigError(
                f"Device model: protocol '{self.protocol}' must be one of "
                f"{sorted(SUPPORTED_PROTOCOLS)}"
            )
        if self.protocol == "ads":
            if self.read_mode is not None and self.read_mode not in ADS_READ_MODES:
                raise ConfigError(
                    f"ADS device model: read_mode must be 'sum' or 'sequential', "
                    f"got '{self.read_mode}'"
                )
        elif self.read_mode is not None:
            raise ConfigError(
                f"Device model (protocol '{self.protocol}'): read_mode is "
                "ADS-specific and must not be configured for other protocols"
            )
        return self


class DeviceModelsFile(BaseModel):
    """``device_models.yaml`` 根模型。"""

    model_config = ConfigDict(extra="forbid")

    device_types: dict[str, DeviceTypeRaw] = Field(default_factory=dict)
    device_models: dict[str, DeviceModelRaw] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_refs(self) -> DeviceModelsFile:
        for model_id, model in self.device_models.items():
            if model.device_type not in self.device_types:
                raise ConfigError(
                    f"Device model '{model_id}' references unknown device_type "
                    f"'{model.device_type}' (available: {sorted(self.device_types)})"
                )
        return self


# ---------------------------------------------------------------------------
# devices.yaml
# ---------------------------------------------------------------------------


class InstanceEndpointRaw(BaseModel):
    """设备实例连接端点——``port`` 可省略，由型号 connection_defaults 提供。"""

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class DeviceInstanceRaw(BaseModel):
    """现场设备实例。"""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    device_id: str
    model: str
    device_group: str | None = None
    endpoint: InstanceEndpointRaw
    enabled: bool = True


class DeviceInstancesFile(BaseModel):
    """``devices.yaml`` 根模型。"""

    model_config = ConfigDict(extra="forbid")

    devices: list[DeviceInstanceRaw]

    @model_validator(mode="after")
    def _validate_unique(self) -> DeviceInstancesFile:
        seen: set[str] = set()
        for device in self.devices:
            if device.device_id in seen:
                raise ConfigError(f"Duplicate device_id: '{device.device_id}'")
            seen.add(device.device_id)
        return self


# ---------------------------------------------------------------------------
# points.yaml
# ---------------------------------------------------------------------------


class PointAddressRaw(BaseModel):
    """协议特有点地址——extra 字段由协议 Driver 按点表 protocol 解释。"""

    model_config = ConfigDict(extra="allow")

    type: str | None = None


class PointConfigRaw(BaseModel):
    """继承展开后的完整点定义。"""

    model_config = ConfigDict(extra="forbid")

    point_id: str
    variable_name: str | None = None
    point_groups: list[str]
    address: PointAddressRaw
    data_type: str = "float32"
    scale: float = 1.0
    offset: float = 0.0
    unit: str = "none"
    description: str | None = None

    @model_validator(mode="after")
    def _validate_point(self) -> PointConfigRaw:
        if not self.point_groups:
            raise ConfigError(f"Point '{self.point_id}': point_groups must be non-empty")
        if len(self.point_groups) != len(set(self.point_groups)):
            raise ConfigError(
                f"Point '{self.point_id}': duplicate point_groups: " f"{self.point_groups}"
            )
        if any(not group.strip() for group in self.point_groups):
            raise ConfigError(f"Point '{self.point_id}': point_groups must be non-empty strings")
        if self.data_type not in ALLOWED_DATA_TYPES:
            raise ConfigError(f"Point '{self.point_id}': unknown data_type '{self.data_type}'")
        return self


class PointPatchRaw(BaseModel):
    """点表继承中的点补丁——以 ``model_fields_set`` 区分「未写」与「显式 null」。"""

    model_config = ConfigDict(extra="forbid")

    point_id: str
    variable_name: str | None = None
    point_groups: list[str] | None = None
    address: PointAddressRaw | None = None
    data_type: str | None = None
    scale: float | None = None
    offset: float | None = None
    unit: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _validate_patch(self) -> PointPatchRaw:
        if not self.point_id:
            raise ConfigError("Point patch: point_id must be non-empty")
        return self


class PointTableRaw(BaseModel):
    """Raw 点表——可复用、可单继承。"""

    model_config = ConfigDict(extra="forbid")

    protocol: str | None = None
    extends: str | None = None
    remove_points: list[str] = Field(default_factory=list)
    points: list[PointPatchRaw] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_raw(self) -> PointTableRaw:
        if self.protocol is not None and self.protocol not in SUPPORTED_PROTOCOLS:
            raise ConfigError(
                f"Point table: protocol '{self.protocol}' must be one of "
                f"{sorted(SUPPORTED_PROTOCOLS)}"
            )
        seen: set[str] = set()
        for patch in self.points:
            if patch.point_id in seen:
                raise ConfigError(f"Duplicate point_id in table: '{patch.point_id}'")
            seen.add(patch.point_id)
        if len(self.remove_points) != len(set(self.remove_points)):
            raise ConfigError(f"Duplicate remove_points: {self.remove_points}")
        return self


class PointTablesFile(BaseModel):
    """``points.yaml`` 根模型。"""

    model_config = ConfigDict(extra="forbid")

    point_tables: dict[str, PointTableRaw] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# units.yaml
# ---------------------------------------------------------------------------


class UnitRaw(BaseModel):
    """单位定义——point.unit 引用 ``units`` 的键。"""

    model_config = ConfigDict(extra="forbid")

    symbol: str = ""
    name: str | None = None


class UnitsFile(BaseModel):
    """``units.yaml`` 根模型。"""

    model_config = ConfigDict(extra="forbid")

    units: dict[str, UnitRaw] = Field(default_factory=dict)


__all__ = [
    "ADS_READ_MODES",
    "ALLOWED_DATA_TYPES",
    "ADSSystemRaw",
    "DeviceInstanceRaw",
    "DeviceInstancesFile",
    "DeviceModelRaw",
    "DeviceModelsFile",
    "DeviceTypeRaw",
    "InstanceEndpointRaw",
    "PointAddressRaw",
    "PointConfigRaw",
    "PointPatchRaw",
    "PointTableRaw",
    "PointTablesFile",
    "SUPPORTED_PROTOCOLS",
    "UnitRaw",
    "UnitsFile",
    "validate_ams_net_id",
]
