"""Collector 进程配置模型。

CollectorConfig 只承载 Collector 真正需要的内容：共享核心领域配置索引
（devices / device_models / point_tables / protocol_options_by_device）、运行时参数
（队列/背压/超时）、采集 Task 定义、resolved Sink 契约、点位元数据
（point_groups / variable_name），以及 ADS 订阅设备集合
（``subscribe_enabled`` 是进程级采集策略，不进协议 Driver 的
protocol_options_by_device——ADS Driver 严格拒绝未知 option）。

本模块是 Application 层的纯配置模型，不感知 YAML/文件细节——解析由
``collector.infrastructure.config`` 完成。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from core.application import ConfigError
from core.application.port import ConfigValue
from core.application.sink_config import ResolvedSinkConfig
from core.domain import (
    Device,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    PointTable,
    PointTableId,
    ProtocolOptions,
)
from core.domain import (
    PointMeta as PointMeta,
)
from core.domain.config import ADSLocalIdentity as ADSLocalIdentity
from core.domain.config import ConfigTopic, TaskConfig
from core.domain.config.lookups import point_table_for_device, protocol_options_for

BackpressurePolicy = Literal["drop_old", "drop_new", "block"]


@dataclass(frozen=True, slots=True)
class RuntimeParams:
    """采集运行时参数（system.yaml ``runtime`` 段的 Collector 子集）。

    Attributes:
        queue_maxsize: 每个 Sink 的有界队列容量（按批次计）。
        backpressure_policy: 队列满时的背压策略。
        shutdown_timeout: 停机阶段等待消费者/资源释放的超时（秒）。
        connect_timeout: 单设备连接（含重连）超时（秒）。
        read_timeout: 应用层单次批量读外层兜底超时（秒）；``None`` 不加外层超时。
    """

    queue_maxsize: int = 1000
    backpressure_policy: BackpressurePolicy = "drop_old"
    shutdown_timeout: float = 10.0
    connect_timeout: float = 10.0
    read_timeout: float | None = None
    reconnect_attempts: int = 1
    write_timeout: float = 5.0

    def __post_init__(self) -> None:
        if self.queue_maxsize <= 0:
            raise ConfigError("queue_maxsize must be > 0")
        if self.backpressure_policy not in ("drop_old", "drop_new", "block"):
            raise ConfigError(
                f"backpressure_policy must be one of drop_old/drop_new/block, "
                f"got '{self.backpressure_policy}'"
            )
        if self.shutdown_timeout <= 0:
            raise ConfigError("shutdown_timeout must be > 0")
        if self.connect_timeout <= 0:
            raise ConfigError("connect_timeout must be > 0")
        if self.reconnect_attempts < 0 or type(self.reconnect_attempts) is not int:
            raise ConfigError("reconnect_attempts must be a nonnegative integer")
        if self.write_timeout <= 0:
            raise ConfigError("write_timeout must be > 0")
        if self.read_timeout is not None and self.read_timeout <= 0:
            raise ConfigError("read_timeout must be > 0")


#: 周期采集 Task 的业务定义——直接使用 Core Domain 的 TaskConfig VO
#: （语义与旧 ``tasks.yaml`` 一致：device/device_group XOR、point_group
#: 必填、interval > 0、targets 非空不重复、enabled 控制运行实例创建）。
CollectionTask = TaskConfig


@dataclass(frozen=True, slots=True)
class DeviceView:
    """单台设备在 Collector 运行时的完整视图（由 CollectorConfig 派生）。

    Attributes:
        device: 共享领域设备聚合。
        point_table: 设备型号绑定的 resolved 点表。
        point_meta: 本点表的进程级点位元数据（按 point_id 索引）。
        options: 合并后的协议参数（model connection_defaults + endpoint
            extensions，ADS read_mode 已注入）。
        subscribe_enabled: 本设备是否使用订阅推送采集（ADS 进程级策略；
            IEC104 恒为订阅、Modbus 恒为轮询，由会话按协议判定）。
        supports_scheduled_collection: 是否支持周期采集（ADS
            ``read_mode='sequential'`` 为单读模式，不支持）。
    """

    device: Device
    point_table: PointTable
    point_meta: Mapping[str, PointMeta]
    options: ProtocolOptions
    subscribe_enabled: bool
    supports_scheduled_collection: bool


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """Collector 启动与 reload 使用的完整配置快照。

    Attributes:
        devices: 启用设备索引（``enabled: false`` 的设备不进索引）。
        device_models: 设备型号索引（设备查找点表用）。
        point_tables: resolved 点表索引。
        protocol_options_by_device: 合并后的协议参数索引（model connection_defaults +
            endpoint extensions，ADS read_mode 已注入）。
        runtime: 运行时参数（队列/背压/超时）。
        tasks: 采集 Task Definition 注册表（``{task_id: CollectionTask}``）。
        sinks: resolved Sink 契约注册表（``{name: ResolvedSinkConfig}``，
            含 disabled——是否实例化由 Runtime 按 ``enabled`` 判定）。
        point_meta: ``{点表: {point_id: PointMeta}}`` 采集分组/展示元数据。
        ads_subscribe_devices: 使用 ADS 订阅推送（notification）的设备集合。
        disabled_devices: 配置级停用设备集合（不进 core 快照，不参与采集）。
        ads_local: 进程级 ADS 本机身份；无 ADS 配置时为 None。
            restart-required——热重载 prepare 阶段拒绝其任何变化。
        configs: 本快照对应的各主题配置 VO（``{ConfigTopic: ConfigValue}``）——
            热重载时由 Core Diff 在 VO 层面计算语义差异的基线。
    """

    configs: Mapping[ConfigTopic, ConfigValue] = field(default_factory=dict)
    devices: Mapping[DeviceId, Device] = field(default_factory=dict)
    device_models: Mapping[DeviceModelId, DeviceModel] = field(default_factory=dict)
    point_tables: Mapping[PointTableId, PointTable] = field(default_factory=dict)
    protocol_options_by_device: Mapping[DeviceId, ProtocolOptions] = field(default_factory=dict)
    runtime: RuntimeParams = field(default_factory=RuntimeParams)
    ads_local: ADSLocalIdentity | None = None
    tasks: Mapping[str, CollectionTask] = field(default_factory=dict)
    sinks: Mapping[str, ResolvedSinkConfig] = field(default_factory=dict)
    point_meta: Mapping[PointTableId, Mapping[str, PointMeta]] = field(default_factory=dict)
    ads_subscribe_devices: frozenset[DeviceId] = frozenset()
    disabled_devices: frozenset[DeviceId] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "configs", MappingProxyType(dict(self.configs)))
        object.__setattr__(self, "devices", MappingProxyType(dict(self.devices)))
        object.__setattr__(self, "device_models", MappingProxyType(dict(self.device_models)))
        object.__setattr__(self, "point_tables", MappingProxyType(dict(self.point_tables)))
        object.__setattr__(
            self,
            "protocol_options_by_device",
            MappingProxyType(dict(self.protocol_options_by_device)),
        )
        object.__setattr__(self, "tasks", MappingProxyType(dict(self.tasks)))
        object.__setattr__(self, "sinks", MappingProxyType(dict(self.sinks)))
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
        object.__setattr__(self, "ads_subscribe_devices", frozenset(self.ads_subscribe_devices))
        object.__setattr__(self, "disabled_devices", frozenset(self.disabled_devices))

    def meta_for(self, table_id: PointTableId, point_id: str) -> PointMeta:
        """返回点位元数据；缺省时返回空元数据（元数据不参与协议路径）。"""
        return self.point_meta.get(table_id, {}).get(
            point_id, PointMeta(variable_name=None, point_groups=())
        )

    def table_meta(self, table_id: PointTableId) -> Mapping[str, PointMeta]:
        """返回整张点表的点位元数据；缺省时返回空映射。"""
        return self.point_meta.get(table_id, MappingProxyType({}))

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

    def device_view(self, device_id: DeviceId) -> DeviceView:
        """派生单台设备的运行时视图。

        Raises:
            ConfigError: 设备不存在于索引（停用设备不在索引内）。
        """
        device = self.devices.get(device_id)
        if device is None:
            raise ConfigError(f"unknown device '{device_id}'")
        point_table = self.point_table_for_device(device_id)
        options = self.protocol_options_for(device_id)
        protocol = point_table.protocol.name
        read_mode = options.get("read_mode")
        return DeviceView(
            device=device,
            point_table=point_table,
            point_meta=self.table_meta(point_table.point_table_id),
            options=options,
            subscribe_enabled=device_id in self.ads_subscribe_devices,
            supports_scheduled_collection=not (protocol == "ads" and read_mode == "sequential"),
        )

    def device_views(self) -> dict[DeviceId, DeviceView]:
        """派生全部设备的运行时视图（启动装配用）。"""
        return {device_id: self.device_view(device_id) for device_id in self.devices}
