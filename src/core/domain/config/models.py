"""配置领域值对象（Config VO）——与配置源无关的统一配置契约。

本模块定义全部配置主题的值对象：不可变、类型明确、维护自身可独立判断
的结构与领域不变量，不依赖 YAML / Pydantic / 文件路径，不包含运行状态
与持久化代码。Collector、Commander 直接引用这些对象。

跨文件引用一致性（设备→型号→点表等）不属于单个 VO 的职责，由共享配置
校验规则统一处理（见 ``core.application.config_service`` 与
``core.infrastructure.config.assembly``）。

不变量违反抛出 :class:`ValueError`；文件解析层负责将其转换为
``ConfigError``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Any

from ..value_objects import DataType

#: 支持的协议标识（点表与设备型号的 protocol 字段值域）。
SUPPORTED_PROTOCOLS = frozenset({"ads", "modbus", "iec104"})

#: ADS 型号 read_mode 值域（ADS 专有，其他协议不得配置）。
ADS_READ_MODES = frozenset({"sum", "sequential"})

ALLOWED_DATA_TYPES = frozenset(data_type.value for data_type in DataType)

_BACKPRESSURE_POLICIES = frozenset({"drop_old", "drop_new", "block"})


class ConfigTopic(StrEnum):
    """配置主题标识；与磁盘文件名的映射由 Infrastructure Adapter 决定。"""

    SYSTEM = "system"
    DEVICE_MODELS = "device_models"
    DEVICES = "devices"
    POINTS = "points"
    UNITS = "units"
    TASKS = "tasks"
    SINKS = "sinks"


def freeze_config_value(value: Any) -> Any:
    """递归复制并冻结配置值：dict → MappingProxyType，list → tuple。"""
    if isinstance(value, dict | MappingProxyType):
        return MappingProxyType({key: freeze_config_value(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze_config_value(item) for item in value)
    return value


def _require_non_empty(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _require_finite(value: float, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{label} must be a finite number")


def _require_protocol(protocol: str, label: str) -> None:
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(
            f"{label}: protocol '{protocol}' must be one of {sorted(SUPPORTED_PROTOCOLS)}"
        )


# ---------------------------------------------------------------------------
# device_models.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceTypeConfig:
    """设备业务类型。"""

    name: str | None


@dataclass(frozen=True, slots=True)
class DeviceModelConfig:
    """设备型号——点表绑定在型号层，同型号全部实例共享。"""

    device_type: str
    manufacturer: str | None
    model: str | None
    protocol: str
    point_table: str
    read_mode: str | None
    properties: Mapping[str, Any]
    connection_defaults: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_non_empty(self.device_type, "Device model device_type")
        _require_protocol(self.protocol, "Device model")
        _require_non_empty(self.point_table, "Device model point_table")
        if self.protocol == "ads":
            if self.read_mode is not None and self.read_mode not in ADS_READ_MODES:
                raise ValueError(
                    "ADS device model: read_mode must be 'sum' or 'sequential', "
                    f"got '{self.read_mode}'"
                )
        elif self.read_mode is not None:
            raise ValueError(
                f"Device model (protocol '{self.protocol}'): read_mode is "
                "ADS-specific and must not be configured for other protocols"
            )
        object.__setattr__(self, "properties", freeze_config_value(self.properties))
        object.__setattr__(
            self, "connection_defaults", freeze_config_value(self.connection_defaults)
        )


@dataclass(frozen=True, slots=True)
class DeviceModelsConfig:
    """``device_models.yaml`` 主题配置。"""

    device_types: Mapping[str, DeviceTypeConfig]
    device_models: Mapping[str, DeviceModelConfig]

    def __post_init__(self) -> None:
        object.__setattr__(self, "device_types", MappingProxyType(dict(self.device_types)))
        object.__setattr__(self, "device_models", MappingProxyType(dict(self.device_models)))


# ---------------------------------------------------------------------------
# devices.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    """设备实例连接端点——``port`` 可省略，由型号 connection_defaults 提供。"""

    host: str
    port: int | None
    extensions: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_non_empty(self.host, "Endpoint host")
        if self.port is not None and (
            isinstance(self.port, bool) or not isinstance(self.port, int)
        ):
            raise ValueError("Endpoint port must be an integer")
        object.__setattr__(self, "extensions", freeze_config_value(self.extensions))


@dataclass(frozen=True, slots=True)
class DeviceInstanceConfig:
    """现场设备实例。"""

    device_id: str
    model: str
    device_group: str | None
    endpoint: EndpointConfig
    enabled: bool

    def __post_init__(self) -> None:
        _require_non_empty(self.device_id, "Device device_id")
        _require_non_empty(self.model, f"Device '{self.device_id}' model")
        if not isinstance(self.enabled, bool):
            raise ValueError(f"Device '{self.device_id}': enabled must be a boolean")


@dataclass(frozen=True, slots=True)
class DevicesConfig:
    """``devices.yaml`` 主题配置。"""

    devices: tuple[DeviceInstanceConfig, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "devices", tuple(self.devices))
        seen: set[str] = set()
        for device in self.devices:
            if device.device_id in seen:
                raise ValueError(f"Duplicate device_id: '{device.device_id}'")
            seen.add(device.device_id)


# ---------------------------------------------------------------------------
# points.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PointConfig:
    """继承展开后的完整点位定义。"""

    point_id: str
    variable_name: str | None
    point_groups: tuple[str, ...]
    address: Mapping[str, Any]
    data_type: str
    scale: float
    offset: float
    unit: str
    description: str | None

    def __post_init__(self) -> None:
        _require_non_empty(self.point_id, "Point point_id")
        object.__setattr__(self, "point_groups", tuple(self.point_groups))
        if not self.point_groups:
            raise ValueError(f"Point '{self.point_id}': point_groups must be non-empty")
        if len(self.point_groups) != len(set(self.point_groups)):
            raise ValueError(
                f"Point '{self.point_id}': duplicate point_groups: {list(self.point_groups)}"
            )
        if any(not isinstance(group, str) or not group.strip() for group in self.point_groups):
            raise ValueError(f"Point '{self.point_id}': point_groups must be non-empty strings")
        if self.data_type not in ALLOWED_DATA_TYPES:
            raise ValueError(f"Point '{self.point_id}': unknown data_type '{self.data_type}'")
        _require_finite(self.scale, f"Point '{self.point_id}' scale")
        _require_finite(self.offset, f"Point '{self.point_id}' offset")
        _require_non_empty(self.unit, f"Point '{self.point_id}' unit")
        object.__setattr__(self, "scale", float(self.scale))
        object.__setattr__(self, "offset", float(self.offset))
        object.__setattr__(self, "address", freeze_config_value(self.address))


@dataclass(frozen=True, slots=True)
class PointTableConfig:
    """一张已完成继承展开、字段完整、可直接交给业务模块使用的点表配置。"""

    protocol: str
    points: Mapping[str, PointConfig]

    def __post_init__(self) -> None:
        _require_protocol(self.protocol, "Point table")
        object.__setattr__(self, "points", MappingProxyType(dict(self.points)))
        for key, point in self.points.items():
            if key != point.point_id:
                raise ValueError(
                    f"Point table points key '{key}' does not match "
                    f"point_id '{point.point_id}'"
                )


@dataclass(frozen=True, slots=True)
class PointTablesConfig:
    """``points.yaml`` 主题配置。"""

    tables: Mapping[str, PointTableConfig]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tables", MappingProxyType(dict(self.tables)))


# ---------------------------------------------------------------------------
# tasks.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskConfig:
    """单条周期采集任务配置（字段级不变量在构造时校验）。"""

    task_id: str
    point_group: str
    targets: tuple[str, ...]
    device: str | None = None
    device_group: str | None = None
    interval: float | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        _require_non_empty(self.task_id, "Collection task task_id")
        if (self.device is None) == (self.device_group is None):
            raise ValueError(
                f"Task '{self.task_id}': exactly one of 'device' / 'device_group' "
                "must be configured (XOR)"
            )
        _require_non_empty(self.point_group, f"Task '{self.task_id}' point_group")
        if self.interval is not None:
            _require_finite(self.interval, f"Task '{self.task_id}' interval")
            if self.interval <= 0:
                raise ValueError(
                    f"Task '{self.task_id}': interval must be > 0, got {self.interval}"
                )
            object.__setattr__(self, "interval", float(self.interval))
        object.__setattr__(self, "targets", tuple(self.targets))
        if not self.targets:
            raise ValueError(f"Task '{self.task_id}': targets must be non-empty")
        if any(not isinstance(sink, str) or not sink.strip() for sink in self.targets):
            raise ValueError(f"Task '{self.task_id}': target sink names must be non-empty")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError(
                f"Task '{self.task_id}': duplicate target sinks: {list(self.targets)}"
            )
        if not isinstance(self.enabled, bool):
            raise ValueError(f"Task '{self.task_id}': enabled must be a boolean")


@dataclass(frozen=True, slots=True)
class TasksConfig:
    """``tasks.yaml`` 主题配置。"""

    tasks: tuple[TaskConfig, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tasks", tuple(self.tasks))
        seen: set[str] = set()
        for task in self.tasks:
            if task.task_id in seen:
                raise ValueError(f"Duplicate task_id: '{task.task_id}'")
            seen.add(task.task_id)


# ---------------------------------------------------------------------------
# system.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SiteIdentity:
    """当前部署实例所属现场的身份（仅标识用途）。"""

    site_id: str
    name: str | None

    def __post_init__(self) -> None:
        _require_non_empty(self.site_id, "site.site_id")


@dataclass(frozen=True, slots=True)
class ADSLocalIdentity:
    """进程级 ADS 本机身份（restart-required，不含凭据）。

    Collector 与 Commander 共享的身份契约；本机 AMS Net ID 绑定路由与
    已建立的 ADS 连接，变化必须重启进程生效。
    """

    local_ams_net_id: str
    local_ip: str


@dataclass(frozen=True, slots=True)
class ADSLocalConfig:
    """system.yaml ``ads`` 段的完整契约：本机身份与凭据（restart-required）。"""

    local_ams_net_id: str
    local_ip: str
    username: str
    password: str

    def __post_init__(self) -> None:
        _require_non_empty(self.local_ams_net_id, "ads.local_ams_net_id")
        _require_non_empty(self.local_ip, "ads.local_ip")


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """跨进程共享的运行参数；``None`` 表示未配置，默认值由各进程自定。"""

    queue_maxsize: int | None = None
    backpressure_policy: str | None = None
    shutdown_timeout: float | None = None
    connect_timeout: float | None = None
    read_timeout: float | None = None
    write_timeout: float | None = None
    reconnect_attempts: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.reconnect_attempts, bool) or not isinstance(self.reconnect_attempts, int):
            raise ValueError("runtime.reconnect_attempts must be an integer")
        if self.reconnect_attempts < 0:
            raise ValueError("runtime.reconnect_attempts must be >= 0")
        if self.queue_maxsize is not None:
            if isinstance(self.queue_maxsize, bool) or not isinstance(self.queue_maxsize, int):
                raise ValueError("runtime.queue_maxsize must be an integer")
            if self.queue_maxsize <= 0:
                raise ValueError("runtime.queue_maxsize must be > 0")
        if self.backpressure_policy is not None and (
            self.backpressure_policy not in _BACKPRESSURE_POLICIES
        ):
            raise ValueError(
                f"Invalid backpressure_policy '{self.backpressure_policy}'; "
                f"must be one of {sorted(_BACKPRESSURE_POLICIES)}"
            )
        for name in ("shutdown_timeout", "connect_timeout", "read_timeout", "write_timeout"):
            value = getattr(self, name)
            if value is None:
                continue
            _require_finite(value, f"runtime.{name}")
            if value <= 0:
                raise ValueError(f"runtime.{name} must be > 0")
            object.__setattr__(self, name, float(value))


@dataclass(frozen=True, slots=True)
class SystemConfig:
    """system.yaml 的共享段；进程专属段（如 interfaces）不进公共契约。"""

    site: SiteIdentity | None = None
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    ads: ADSLocalConfig | None = None


# ---------------------------------------------------------------------------
# units.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitDefinitionConfig:
    """单位定义——point.unit 引用 ``units`` 的键。"""

    symbol: str
    name: str | None


@dataclass(frozen=True, slots=True)
class UnitsConfig:
    """``units.yaml`` 主题配置。"""

    units: Mapping[str, UnitDefinitionConfig]

    def __post_init__(self) -> None:
        object.__setattr__(self, "units", MappingProxyType(dict(self.units)))


__all__ = [
    "ADS_READ_MODES",
    "ALLOWED_DATA_TYPES",
    "SUPPORTED_PROTOCOLS",
    "ADSLocalConfig",
    "ADSLocalIdentity",
    "ConfigTopic",
    "DeviceInstanceConfig",
    "DeviceModelConfig",
    "DeviceModelsConfig",
    "DeviceTypeConfig",
    "DevicesConfig",
    "EndpointConfig",
    "PointConfig",
    "PointTableConfig",
    "PointTablesConfig",
    "RuntimeSettings",
    "SiteIdentity",
    "SystemConfig",
    "TaskConfig",
    "TasksConfig",
    "UnitDefinitionConfig",
    "UnitsConfig",
    "freeze_config_value",
]
