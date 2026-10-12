"""Commander 进程配置模型。

CommanderConfig 只承载 Commander 真正需要的内容：共享核心领域配置索引
（设备、型号、分组、点表、业务点、协议参数）、进程级 ADS 本机身份、
连接/写超时，以及直接来自 Point 的诊断字段。
Task、Sink、Reporting 等 Collector/Server 配置不属于
本模型。

本模块是 Application 层的纯配置模型，不感知 YAML/文件细节——解析由
``commander.infrastructure.config`` 完成。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from core.application.settings import ADSLocalConfig
from core.domain import (
    BusinessPoint,
    BusinessPointId,
    Device,
    DeviceGroup,
    DeviceGroupId,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    PointTable,
    PointTableId,
    ProtocolOptions,
)
from core.domain.unit import UnitCatalog


@dataclass(frozen=True, slots=True)
class CommanderConfig:
    """Commander 启动与 reload 使用的完整配置快照。

    Attributes:
        devices: 启用设备索引（``enabled: false`` 的设备不进索引）。
        device_models: 设备型号索引（设备查找点表用）。
        device_groups: 设备分组索引。
        point_tables: resolved 点表索引。
        business_points: 业务点索引（诊断展示单位/数据类型）。
        protocol_options_by_device: 合并后的协议参数索引。
        ads_local: 进程级 ADS 本机身份；无 ADS 配置时为 None。
        connect_timeout: 单设备连接超时（秒）。
        write_timeout: 默认写超时（秒）；Command.timeout <= 0 时生效。
        disabled_devices: 配置级停用设备集合；停用设备不参与即时操作。
        units: 工程单位目录；restart-required——热重载拒绝其任何变化。
    """

    units: UnitCatalog = field(default_factory=lambda: UnitCatalog(units={}, base_units={}))
    devices: Mapping[DeviceId, Device] = field(default_factory=dict)
    device_models: Mapping[DeviceModelId, DeviceModel] = field(default_factory=dict)
    device_groups: Mapping[DeviceGroupId, DeviceGroup] = field(default_factory=dict)
    point_tables: Mapping[PointTableId, PointTable] = field(default_factory=dict)
    business_points: Mapping[BusinessPointId, BusinessPoint] = field(default_factory=dict)
    protocol_options_by_device: Mapping[DeviceId, ProtocolOptions] = field(default_factory=dict)
    ads_local: ADSLocalConfig | None = None
    connect_timeout: float = 1.0
    write_timeout: float = 1.0
    read_timeout: float = 1.0
    watchdog_interval: float = 5.0
    health_timeout: float = 1.0
    disabled_devices: frozenset[DeviceId] = frozenset()

    def __post_init__(self) -> None:
        for name in (
            "connect_timeout",
            "write_timeout",
            "read_timeout",
            "watchdog_interval",
            "health_timeout",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        object.__setattr__(self, "disabled_devices", frozenset(self.disabled_devices))
        for name in (
            "devices",
            "device_models",
            "device_groups",
            "point_tables",
            "business_points",
            "protocol_options_by_device",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    def point_table_for_device(self, device_id: DeviceId) -> PointTable:
        """解析 Device -> DeviceModel -> PointTable。"""
        device = self.devices[device_id]
        model = self.device_models[device.device_model_id]
        return self.point_tables[model.point_table_id]

    def protocol_options_for(self, device_id: DeviceId) -> ProtocolOptions:
        """返回指定 Device 的协议专有连接配置；缺省为空映射。"""
        return self.protocol_options_by_device.get(device_id, MappingProxyType({}))
