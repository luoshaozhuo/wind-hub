"""Collector 进程配置模型。

CollectorConfig 只承载 Collector 真正需要的内容：共享核心领域配置索引
（devices / device_models / point_tables）、运行时参数
（队列/背压/超时）、采集 Task 定义、Sink 契约、测点分组信息
（直接由 Core Point 提供）；订阅采集策略由 Device 保存，不透传给 Driver。

本模块是 Application 层的纯配置模型，不感知 YAML/文件细节——解析由
``collector.infrastructure.config`` 完成。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from core.application import ConfigError
from core.application.settings import ADSLocalConfig
from core.domain import (
    Device,
    DeviceId,
    DeviceModel,
    DeviceModelId,
    PointTable,
    PointTableId,
    Sink,
    Task,
)
from core.domain.unit import UnitCatalog

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
        thread_pool_max_workers: 进程级协议命令线程池大小（restart-required）。
    """

    queue_maxsize: int = 1000
    backpressure_policy: BackpressurePolicy = "drop_old"
    shutdown_timeout: float = 10.0
    connect_timeout: float = 1.0
    read_timeout: float | None = 1.0
    write_timeout: float = 1.0
    watchdog_interval: float = 5.0
    health_timeout: float = 1.0
    thread_pool_max_workers: int = 32

    def __post_init__(self) -> None:
        if self.queue_maxsize <= 0:
            raise ConfigError("queue_maxsize must be > 0")
        if self.backpressure_policy not in ("drop_old", "drop_new", "block"):
            raise ConfigError(
                f"backpressure_policy must be one of drop_old/drop_new/block, "
                f"got '{self.backpressure_policy}'"
            )
        for name in (
            "shutdown_timeout",
            "connect_timeout",
            "watchdog_interval",
            "health_timeout",
            "write_timeout",
        ):
            if getattr(self, name) <= 0:
                raise ConfigError(f"{name} must be > 0")
        if self.read_timeout is not None and self.read_timeout <= 0:
            raise ConfigError("read_timeout must be > 0")


CollectionTask = Task


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    """Collector 启动与 reload 使用的完整配置快照。

    Attributes:
        devices: 启用设备索引（``enabled: false`` 的设备不进索引）。
        device_models: 设备型号索引（设备查找点表用）。
        point_tables: resolved 点表索引。
        runtime: 运行时参数（队列/背压/超时）。
        tasks: 采集 Task Definition 注册表（``{task_id: CollectionTask}``）。
        sinks: Sink 注册表（``{name: Sink}``，
            含 disabled——是否实例化由 Runtime 按 ``enabled`` 判定）。
        disabled_devices: 配置级停用设备集合（不进 core 快照，不参与采集）。
        ads_local: 进程级 ADS 本机身份；无 ADS 配置时为 None。
            restart-required——热重载 prepare 阶段拒绝其任何变化。
        units: 工程单位目录；restart-required——单位定义变化会改变已采集
            数据的语义，热重载 prepare 阶段拒绝其任何变化。
    """

    units: UnitCatalog
    devices: Mapping[DeviceId, Device] = field(default_factory=dict)
    device_models: Mapping[DeviceModelId, DeviceModel] = field(default_factory=dict)
    point_tables: Mapping[PointTableId, PointTable] = field(default_factory=dict)
    runtime: RuntimeParams = field(default_factory=RuntimeParams)
    ads_local: ADSLocalConfig | None = None
    tasks: Mapping[str, CollectionTask] = field(default_factory=dict)
    sinks: Mapping[str, Sink] = field(default_factory=dict)
    disabled_devices: frozenset[DeviceId] = frozenset()

    def __post_init__(self) -> None:
        for name in (
            "devices",
            "device_models",
            "point_tables",
            "tasks",
            "sinks",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        object.__setattr__(self, "disabled_devices", frozenset(self.disabled_devices))
        # 在任何连接建立或热重载副作用前拒绝目标 AMS 身份冲突。
        ads_targets: dict[str, str] = {}
        for device_id, device in self.devices.items():
            model = self.device_models[device.device_model_id]
            table = self.point_tables[model.point_table_id]
            if table.protocol.name != "ads":
                continue
            target = device.protocol_options.get("target_net_id")
            if not isinstance(target, str) or not target:
                raise ConfigError(f"ADS device '{device_id}' requires an explicit target_net_id")
            previous = ads_targets.get(target)
            if previous is not None:
                raise ConfigError(
                    f"ADS target_net_id '{target}' is shared by devices "
                    f"'{previous}' and '{device_id}'"
                )
            ads_targets[target] = str(device_id)

    def point_table_for_device(self, device_id: DeviceId) -> PointTable:
        """解析 Device -> DeviceModel -> PointTable。"""
        device = self.devices[device_id]
        model = self.device_models[device.device_model_id]
        return self.point_tables[model.point_table_id]

