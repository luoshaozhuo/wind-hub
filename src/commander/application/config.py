"""Commander 进程配置模型。

CommanderConfig 只承载 Commander 真正需要的内容：共享核心领域快照
（``core``）、进程级 ADS 本机身份、连接/写超时，以及诊断展示所需的
点位元数据（variable_name / point_groups）。Task、Sink、Reporting 等
Collector/Server 配置不属于本模型。

本模块是 Application 层的纯配置模型，不感知 YAML/文件细节——解析由
``commander.infrastructure.config`` 完成。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from core.domain import CoreConfigSnapshot, DeviceId, PointTableId


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
        core: 共享核心领域配置快照（设备、型号、点表、业务点、协议参数）。
        ads_local: 进程级 ADS 本机身份；无 ADS 配置时为 None。
        connect_timeout: 单设备连接超时（秒）。
        write_timeout: 默认写超时（秒）；Command.timeout <= 0 时生效。
        point_meta: ``{点表: {point_id: PointMeta}}`` 诊断展示元数据。
        disabled_devices: 配置级停用设备集合；停用设备不参与即时操作。
    """

    core: CoreConfigSnapshot
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

    def meta_for(self, table_id: PointTableId, point_id: str) -> PointMeta:
        """返回点位元数据；缺省时返回空元数据（元数据不参与协议路径）。"""
        return self.point_meta.get(table_id, {}).get(
            point_id, PointMeta(variable_name=None, point_groups=())
        )
