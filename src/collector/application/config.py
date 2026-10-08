"""Collector 进程配置模型。

CollectorConfig 只承载 Collector 真正需要的内容：共享核心领域快照
（``core``）、运行时参数（队列/背压/超时）、采集 Task 定义、resolved
Sink 契约、点位元数据（point_groups / variable_name），以及 ADS 订阅
设备集合（``subscribe_enabled`` 是进程级采集策略，不进协议 Driver 的
device_options——ADS Driver 严格拒绝未知 option）。

本模块是 Application 层的纯配置模型，不感知 YAML/文件细节——解析由
``collector.infrastructure.config`` 完成。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from core.application import ConfigError
from core.domain import (
    CoreConfigSnapshot,
    Device,
    DeviceId,
    PointTable,
    PointTableId,
    ProtocolOptions,
)

from .sinks import ResolvedSinkConfig

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
        if self.read_timeout is not None and self.read_timeout <= 0:
            raise ConfigError("read_timeout must be > 0")


@dataclass(frozen=True, slots=True)
class ADSLocalIdentity:
    """进程级 ADS 本机身份（system.yaml ``ads`` 段的 Collector 子集）。

    属于进程级启动配置：本机 AMS Net ID 绑定路由与已建立的 ADS 连接，
    不支持热重载——变化在 prepare 阶段拒绝，必须重启进程生效。

    Application 层不直接引用 ``core.infrastructure`` 的 ADSLocalConfig；
    组合根在装配时把本模型转换为 Core 的 ADSLocalConfig。
    """

    local_ams_net_id: str
    local_ip: str


@dataclass(frozen=True, slots=True)
class PointMeta:
    """点位的进程级元数据（采集分组与展示用，不属于协议寻址）。

    Attributes:
        variable_name: 业务变量名（状态/诊断输出展示）。
        point_groups: 采集分组集合——Task 按 point_group 选点。
    """

    variable_name: str | None
    point_groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CollectionTask:
    """周期采集 Task 的业务定义（Task Definition，不可变快照）。

    语义与旧 ``tasks.yaml`` 一致：

    - ``device`` / ``device_group`` 二选一（XOR）——选择设备范围；
    - ``point_group`` 单值必填——命中点位 ``point_groups`` 含该值的全部点；
    - ``interval`` 为采集节拍（秒，> 0）——主动轮询（Modbus、ADS Sum）
      作为 fixed-rate 采样周期，ADS 订阅作为 notification cycle_time；
      纯 IEC104 订阅 Task 可为 ``None``；
    - ``targets`` 为输出目标 Sink 名列表（至少一个，不允许重复）；
    - ``enabled: false`` 时 Runtime 不创建运行实例。
    """

    task_id: str
    point_group: str
    targets: tuple[str, ...]
    device: str | None = None
    device_group: str | None = None
    interval: float | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ConfigError("Collection task: task_id must be non-empty")
        if (self.device is None) == (self.device_group is None):
            raise ConfigError(
                f"Task '{self.task_id}': exactly one of 'device' / 'device_group' "
                "must be configured (XOR)"
            )
        if not self.point_group.strip():
            raise ConfigError(f"Task '{self.task_id}': point_group must be non-empty")
        if self.interval is not None and self.interval <= 0:
            raise ConfigError(f"Task '{self.task_id}': interval must be > 0, got {self.interval}")
        object.__setattr__(self, "targets", tuple(self.targets))
        if not self.targets:
            raise ConfigError(f"Task '{self.task_id}': targets must be non-empty")
        if any(not sink.strip() for sink in self.targets):
            raise ConfigError(f"Task '{self.task_id}': target sink names must be non-empty")
        if len(set(self.targets)) != len(self.targets):
            raise ConfigError(
                f"Task '{self.task_id}': duplicate target sinks: {list(self.targets)}"
            )


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
        core: 共享核心领域配置快照（设备、型号、点表、业务点、协议参数）。
        runtime: 运行时参数（队列/背压/超时）。
        tasks: 采集 Task Definition 注册表（``{task_id: CollectionTask}``）。
        sinks: resolved Sink 契约注册表（``{name: ResolvedSinkConfig}``，
            含 disabled——是否实例化由 Runtime 按 ``enabled`` 判定）。
        point_meta: ``{点表: {point_id: PointMeta}}`` 采集分组/展示元数据。
        ads_subscribe_devices: 使用 ADS 订阅推送（notification）的设备集合。
        disabled_devices: 配置级停用设备集合（不进 core 快照，不参与采集）。
        ads_local: 进程级 ADS 本机身份；无 ADS 配置时为 None。
            restart-required——热重载 prepare 阶段拒绝其任何变化。
    """

    core: CoreConfigSnapshot
    runtime: RuntimeParams = field(default_factory=RuntimeParams)
    ads_local: ADSLocalIdentity | None = None
    tasks: Mapping[str, CollectionTask] = field(default_factory=dict)
    sinks: Mapping[str, ResolvedSinkConfig] = field(default_factory=dict)
    point_meta: Mapping[PointTableId, Mapping[str, PointMeta]] = field(default_factory=dict)
    ads_subscribe_devices: frozenset[DeviceId] = frozenset()
    disabled_devices: frozenset[DeviceId] = frozenset()

    def __post_init__(self) -> None:
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

    def device_view(self, device_id: DeviceId) -> DeviceView:
        """派生单台设备的运行时视图。

        Raises:
            ConfigError: 设备不存在于快照（停用设备不在快照内）。
        """
        device = self.core.devices.get(device_id)
        if device is None:
            raise ConfigError(f"unknown device '{device_id}'")
        point_table = self.core.point_table_for_device(device_id)
        options = self.core.device_options_for(device_id)
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
        return {device_id: self.device_view(device_id) for device_id in self.core.devices}
