"""Commander 进程配置模型。

CommanderConfig 只承载 Commander 真正需要的内容：共享核心领域配置索引
（设备、型号、分组、点表、业务点、协议参数）、进程级 ADS 本机身份、
连接/写超时，以及诊断展示所需的点位元数据（variable_name /
point_groups）。Task、Sink、Reporting 等 Collector/Server 配置不属于
本模型。

本模块是 Application 层的纯配置模型，不感知 YAML/文件细节——解析由
``commander.infrastructure.config`` 完成。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

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
from core.domain import PointMeta as PointMeta
from core.domain.config import ADSLocalIdentity as ADSLocalIdentity
from core.domain.config import ConfigTopic
from core.domain.config.lookups import point_table_for_device, protocol_options_for

# Commander 实际消费的配置主题；tasks/sinks 等无关文件不解析，其
# 变更也不影响本进程的配置一致性检查。
COMMANDER_CONFIG_TOPICS: tuple[ConfigTopic, ...] = (
    ConfigTopic.SYSTEM,
    ConfigTopic.DEVICE_MODELS,
    ConfigTopic.DEVICES,
    ConfigTopic.POINTS,
    ConfigTopic.UNITS,
)


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
        point_meta: ``{点表: {point_id: PointMeta}}`` 诊断展示元数据。
        disabled_devices: 配置级停用设备集合；停用设备不参与即时操作。
    """

    devices: Mapping[DeviceId, Device] = field(default_factory=dict)
    device_models: Mapping[DeviceModelId, DeviceModel] = field(default_factory=dict)
    device_groups: Mapping[DeviceGroupId, DeviceGroup] = field(default_factory=dict)
    point_tables: Mapping[PointTableId, PointTable] = field(default_factory=dict)
    business_points: Mapping[BusinessPointId, BusinessPoint] = field(default_factory=dict)
    protocol_options_by_device: Mapping[DeviceId, ProtocolOptions] = field(default_factory=dict)
    ads_local: ADSLocalIdentity | None = None
    connect_timeout: float = 1.0
    write_timeout: float = 1.0
    read_timeout: float = 1.0
    reconnect_attempts: int = 1
    point_meta: Mapping[PointTableId, Mapping[str, PointMeta]] = field(default_factory=dict)
    disabled_devices: frozenset[DeviceId] = frozenset()

    def __post_init__(self) -> None:
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be > 0")
        if self.write_timeout <= 0:
            raise ValueError("write_timeout must be > 0")
        if self.read_timeout <= 0:
            raise ValueError("read_timeout must be > 0")
        if self.reconnect_attempts < 0 or type(self.reconnect_attempts) is not int:
            raise ValueError("reconnect_attempts must be a nonnegative integer")
        object.__setattr__(
            self,
            "point_meta",
            MappingProxyType(
                {
                    table_id: MappingProxyType(dict(meta))
                    for table_id, meta in self.point_meta.items()
                }
            ),
        )
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

    def meta_for(self, table_id: PointTableId, point_id: str) -> PointMeta:
        """返回点位元数据；缺省时返回空元数据（元数据不参与协议路径）。"""
        return self.point_meta.get(table_id, {}).get(
            point_id, PointMeta(variable_name=None, point_groups=())
        )

    def point_table_for_device(self, device_id: DeviceId) -> PointTable:
        """解析 Device -> DeviceModel -> PointTable。"""
        return point_table_for_device(
            self.devices,
            self.device_models,
            self.point_tables,
            device_id,
        )

    def protocol_options_for(self, device_id: DeviceId) -> ProtocolOptions:
        """返回指定 Device 的协议专有连接配置。"""
        return protocol_options_for(self.protocol_options_by_device, device_id)
