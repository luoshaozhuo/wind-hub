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
from core.domain.config.lookups import device_options_for, point_table_for_device


@dataclass(frozen=True, slots=True)
class ADSLocalIdentity:
    """进程级 ADS 本机身份（system.yaml ``ads`` 段的 Commander 子集）。

    Application 层不直接引用 ``core.infrastructure`` 的 ADSLocalConfig；
    组合根在装配时把本模型转换为 Core 的 ADSLocalConfig。
    """

    local_ams_net_id: str
    local_ip: str


@dataclass(frozen=True, slots=True)
class PointMeta:
    """点位的进程级元数据（诊断/展示用，不属于协议寻址）。

    Attributes:
        variable_name: 业务变量名（诊断输出展示）。
        point_groups: 采集分组集合；诊断按 point_group 批量验证时使用。
    """

    variable_name: str | None
    point_groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommanderConfig:
    """Commander 启动与 reload 使用的完整配置快照。

    Attributes:
        devices: 启用设备索引（``enabled: false`` 的设备不进索引）。
        device_models: 设备型号索引（设备查找点表用）。
        device_groups: 设备分组索引。
        point_tables: resolved 点表索引。
        business_points: 业务点索引（诊断展示单位/数据类型）。
        device_options: 合并后的协议参数索引。
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
    device_options: Mapping[DeviceId, ProtocolOptions] = field(default_factory=dict)
    ads_local: ADSLocalIdentity | None = None
    connect_timeout: float = 10.0
    write_timeout: float = 5.0
    point_meta: Mapping[PointTableId, Mapping[str, PointMeta]] = field(default_factory=dict)
    disabled_devices: frozenset[DeviceId] = frozenset()

    def __post_init__(self) -> None:
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be > 0")
        if self.write_timeout <= 0:
            raise ValueError("write_timeout must be > 0")
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
            "device_options",
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

    def device_options_for(self, device_id: DeviceId) -> ProtocolOptions:
        """返回指定 Device 的协议专有连接配置。"""
        return device_options_for(self.device_options, device_id)
