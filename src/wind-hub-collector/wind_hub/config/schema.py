"""Wind Hub Collector 专属配置 schema 与聚合配置。

跨进程共享的设备、点表、单位和现场配置模型来自 wind-hub-core。本模块只保留
Collector 的 Runtime、Sink、Task、Reporting 与完整 Config 聚合语义；不重复定义
公共模型，也不执行网络 I/O。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wind_hub_core.config.schema import (
    ADSSystemConfig,
    ALLOWED_DATA_TYPES,
    NUMERIC_DATA_TYPES,
    SUPPORTED_PROTOCOLS,
    DeviceConfig,
    DeviceInstanceConfig,
    DeviceInstancesConfig,
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    DeviceTypeConfig,
    InstanceEndpoint,
    PointAddress,
    PointConfig,
    PointPatch,
    PointTableConfig,
    PointTablesConfig,
    ResolvedPointTable,
    ResolvedPointTables,
    SiteConfig,
    UnitConfig,
    UnitsConfig,
)
from wind_hub_core.model.errors import ConfigError

class RuntimeConfig(BaseModel):
    """Runtime 队列、背压和超时参数。"""

    model_config = ConfigDict(extra="forbid")

    queue_maxsize: int = 1000
    """每个 Sink queue 的容量；满时按 backpressure_policy 处理。"""

    backpressure_policy: str = "drop_old"
    """Sink queue 满时的背压策略。

    drop_new 丢弃新批次；drop_old 淘汰旧批次；block 阻塞采集直到队列有空间。
    """

    shutdown_timeout: float = 30.0
    """优雅停机等待在途操作完成的最大时间，单位秒。"""

    connect_timeout: float = 10.0
    """单设备连接超时，单位秒。"""

    read_timeout: float = 5.0
    """单次批量读的应用层兜底超时，单位秒；协议 Driver 内部仍保留底层超时。"""

    write_timeout: float = 5.0
    """默认写超时，单位秒；Command.timeout <= 0 时由 CommandDispatcher 使用。"""

    @model_validator(mode="after")
    def _validate_backpressure(self) -> RuntimeConfig:
        allowed = {"drop_old", "drop_new", "block"}
        if self.backpressure_policy not in allowed:
            raise ConfigError(
                f"Invalid backpressure_policy '{self.backpressure_policy}'; "
                f"must be one of {sorted(allowed)}"
            )
        return self


class SinkConfig(BaseModel):
    """单个数据 Sink 定义。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    """Sink 唯一名称，由 TaskTarget.sink 引用。"""

    type: str
    """Sink 类型：kafka、file 或 db。"""

    enabled: bool = True
    """是否启用该 Sink。"""

    params: dict[str, Any] = Field(default_factory=dict)
    """Sink 实现特有参数；动态字段由对应 adapter 创建时校验。"""


class ApiConfig(BaseModel):
    """兼容保留的 HTTP Server 设置；Collector 独立进程不消费该字段。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080


class CliConfig(BaseModel):
    """兼容保留的 CLI 设置；wind-hub-ctl 不从该配置读取连接目标。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class InterfaceConfig(BaseModel):
    """兼容保留的入站适配器设置。"""

    model_config = ConfigDict(extra="forbid")

    api: ApiConfig = Field(default_factory=ApiConfig)
    cli: CliConfig = Field(default_factory=CliConfig)


class SystemConfig(BaseModel):
    """system.yaml 顶层配置。"""

    model_config = ConfigDict(extra="forbid")

    site: SiteConfig | None = None
    """当前现场身份；``None`` 表示未声明（仅标识用途，不影响运行链路）。"""
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    ads: ADSSystemConfig | None = None
    """进程级 ADS 本机配置；为 ``None`` 时（或无 ADS 设备）不做 ADS 本机初始化。"""
    sinks: list[SinkConfig] = Field(default_factory=list)
    interfaces: InterfaceConfig = Field(default_factory=InterfaceConfig)

    @model_validator(mode="after")
    def _validate_sinks(self) -> SystemConfig:
        sink_names = [s.name for s in self.sinks]
        if len(sink_names) != len(set(sink_names)):
            raise ConfigError(f"Duplicate sink names: {sink_names}")
        return self


# ---------------------------------------------------------------------------
# tasks.yaml — 周期采集任务（Task Definition）
# ---------------------------------------------------------------------------


class TaskTarget(BaseModel):
    """采集 Task 的输出目标——只引用 Sink 名称。

    Sink 实例与连接参数定义在 ``system.yaml`` 的 ``sinks`` 中；Task 不复制
    任何连接配置。
    """

    model_config = ConfigDict(extra="forbid")

    sink: str
    """目标 Sink 名（``system.yaml`` 中 ``SinkConfig.name``）。"""


class CollectionTaskConfig(BaseModel):
    """周期采集 Task 的业务定义（配置层 Task Definition）。

    语义：

    - ``device`` / ``device_group`` 二选一（XOR）——选择设备范围；
    - ``point_group`` 单值必填——选择点位范围（匹配
      ``PointConfig.point_groups`` 多值集合）；
    - ``interval`` 为采集节拍（秒，> 0）——主动轮询协议（Modbus、ADS
      Sum）作为 fixed-rate 采样周期，ADS 订阅作为 notification
      cycle_time；纯 IEC104 订阅 Task 可不配置（数据到达时机由远端
      spontaneous / periodic 决定）。是否必填由加载期跨文件校验按
      命中设备的协议能力判定；
    - ``targets`` 决定采集结果输出到哪些 Sink；
    - ``enabled`` 是配置级能力开关：``False`` 时 Runtime 不创建运行实例。

    ``device_group`` Task 在 Runtime 展开为每台命中设备一个 Task Instance
    （见 ``application/runtime``）。
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    device: str | None = None
    """目标单台设备（``device_id``）；与 ``device_group`` 互斥。"""
    device_group: str | None = None
    """目标设备业务类别；与 ``device`` 互斥。"""
    point_group: str
    """点位分组（单值）——命中 ``point_groups`` 含该值的全部点位。"""
    interval: float | None = None
    """采集节拍（秒，配置时必须 > 0）。主动轮询与 ADS 订阅必填；
    纯 IEC104 订阅 Task 可省略。"""
    targets: list[TaskTarget]
    """输出目标 Sink 列表（至少一个，不允许重复）。"""
    enabled: bool = True

    @model_validator(mode="after")
    def _validate_task(self) -> CollectionTaskConfig:
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
        if not self.targets:
            raise ConfigError(f"Task '{self.task_id}': targets must be non-empty")
        sink_names = [t.sink for t in self.targets]
        if len(sink_names) != len(set(sink_names)):
            raise ConfigError(f"Task '{self.task_id}': duplicate target sinks: {sink_names}")
        return self


class TasksConfig(BaseModel):
    """tasks.yaml 顶层 Task Definition 集。"""

    model_config = ConfigDict(extra="forbid")

    tasks: list[CollectionTaskConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_tasks(self) -> TasksConfig:
        seen: set[str] = set()
        for t in self.tasks:
            if t.task_id in seen:
                raise ConfigError(f"Duplicate task_id: '{t.task_id}'")
            seen.add(t.task_id)
        return self


# ---------------------------------------------------------------------------
# reporting.yaml — IEC104 slave proxy (从站模式)
# ---------------------------------------------------------------------------

ALLOWED_REPORTING_DATA_TYPES = frozenset(
    {
        "M_SP_NA_1",
        "M_DP_NA_1",
        "M_ME_NA_1",
        "M_ME_NB_1",
        "M_ME_NC_1",
        "M_SP_TB_1",
        "M_DP_TB_1",
        "M_ME_TF_1",
    }
)


class ReportingPoint(BaseModel):
    """通过 IEC104 slave proxy 暴露给调度主站的单个点。

    把 Collector 内部 (device_id, point_id) 映射为 IOA 和监视方向 TypeID。
    """

    model_config = ConfigDict(extra="forbid")

    device_id: str
    """设备标识。"""

    point_id: str
    """设备点表内 point_id。"""

    ioa: int
    """Information Object Address，范围 0~0xFFFFFF。"""

    data_type: str
    """监视方向 ASDU TypeID，例如 M_ME_NC_1。"""

    @model_validator(mode="after")
    def _validate_reporting_point(self) -> ReportingPoint:
        if not 0 <= self.ioa <= 0xFFFFFF:
            raise ConfigError(
                f"Reporting point '{self.device_id}/{self.point_id}': "
                f"ioa {self.ioa:#x} out of range [0, 0xFFFFFF]"
            )
        if self.data_type not in ALLOWED_REPORTING_DATA_TYPES:
            raise ConfigError(
                f"Reporting point '{self.device_id}/{self.point_id}': "
                f"data_type '{self.data_type}' must be one of "
                f"{sorted(ALLOWED_REPORTING_DATA_TYPES)}"
            )
        return self


class ReportingConfig(BaseModel):
    """reporting.yaml 顶层 IEC104 slave proxy 配置。"""

    model_config = ConfigDict(extra="forbid")

    reporting: list[ReportingPoint] = Field(default_factory=list)
    """需要向调度主站暴露的点列表。"""

    batch_size: int = 50
    """总召数据单批 information object 数量软上限；253-byte APDU 是硬上限。"""

    common_address: int = 1
    """IEC104 公共地址/站地址。"""

    host: str = "127.0.0.1"
    """slave proxy TCP 监听地址。"""

    port: int = 12404
    """slave proxy TCP 监听端口。"""

    @model_validator(mode="after")
    def _validate_reporting(self) -> ReportingConfig:
        if self.batch_size <= 0:
            raise ConfigError(f"batch_size must be >= 1, got {self.batch_size}")
        if not 0 <= self.common_address <= 0xFFFF:
            raise ConfigError(f"common_address {self.common_address} out of range [0, 0xFFFF]")
        if not 1 <= self.port <= 0xFFFF:
            raise ConfigError(f"port {self.port} out of range [1, 0xFFFF]")
        seen_points: set[tuple[str, str]] = set()
        seen_ioas: set[int] = set()
        for p in self.reporting:
            key = (p.device_id, p.point_id)
            if key in seen_points:
                raise ConfigError(
                    f"Duplicate reporting (device_id, point_id): ('{p.device_id}', '{p.point_id}')"
                )
            seen_points.add(key)
            if p.ioa in seen_ioas:
                raise ConfigError(f"Duplicate reporting ioa: {p.ioa:#x}")
            seen_ioas.add(p.ioa)
        return self


# ---------------------------------------------------------------------------
# 顶层聚合配置
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """完成加载、继承展开和跨文件校验后的 Collector 配置快照。

    组合根和 Runtime 只消费本对象，不再读取原始 YAML。
    """

    model_config = ConfigDict(extra="forbid")

    system: SystemConfig
    units: UnitsConfig
    """单位定义集（``units.yaml``）——``PointConfig.unit`` 引用的 unit ID
    命名空间；展示层经 ``units[unit_id].symbol`` 取显示符号。"""
    device_types: dict[str, DeviceTypeConfig] = Field(default_factory=dict)
    """公共设备类型定义（``device_models.yaml``）——仅业务分类元数据。"""
    device_models: dict[str, DeviceModelConfig] = Field(default_factory=dict)
    """公共设备型号定义——运行链路不直接消费（设备已 resolve 为
    :class:`DeviceConfig`），保留用于 diff、诊断与导出。"""
    devices: DevicesConfig
    point_tables: ResolvedPointTables
    """继承解析完成后的点表集——运行链路只使用 resolved 模型。"""
    tasks: TasksConfig
    """周期采集 Task 定义集——没有 Task 就不进行周期采集。"""
    reporting: ReportingConfig | None = None
    """可选 IEC104 slave proxy 配置；None 表示不启用。"""

    def points_for_device(self, device_id: str) -> list[PointConfig]:
        """解析设备绑定点表的点集。

        每次调用返回**新的 list**（浅拷贝）：配置加载后即不可变快照，
        调用方（Runtime/引擎/处理器注入）拿到的副本可安全持有，任何
        「原地修改点表」都不会污染配置快照，也不会影响其他绑定同一表
        的设备。共享语义体现在「引用同一表定义、内容一致」，而非共享
        同一个 Python list 对象。

        Raises:
            KeyError: 设备或其绑定的点表不存在（loader 交叉校验保证
                加载后的配置不会出现此情况）。
        """
        device = next(d for d in self.devices.devices if d.device_id == device_id)
        return list(self.point_tables.tables[device.point_table].points)

    def points_by_device(self) -> dict[str, list[PointConfig]]:
        """``{device_id: 点集}``——每个键都是独立 list（见
        :meth:`points_for_device` 的快拍语义）。"""
        return {d.device_id: self.points_for_device(d.device_id) for d in self.devices.devices}


__all__ = [
    "ADSSystemConfig",
    "SiteConfig",
    "UnitConfig",
    "UnitsConfig",
    "DeviceTypeConfig",
    "DeviceModelConfig",
    "DeviceModelsConfig",
    "InstanceEndpoint",
    "DeviceInstanceConfig",
    "DeviceInstancesConfig",
    "DeviceConfig",
    "DevicesConfig",
    "PointAddress",
    "PointConfig",
    "PointPatch",
    "PointTableConfig",
    "PointTablesConfig",
    "ResolvedPointTable",
    "ResolvedPointTables",
    "RuntimeConfig",
    "SinkConfig",
    "ApiConfig",
    "CliConfig",
    "InterfaceConfig",
    "SystemConfig",
    "TaskTarget",
    "CollectionTaskConfig",
    "TasksConfig",
    "ReportingPoint",
    "ReportingConfig",
    "Config",
]
