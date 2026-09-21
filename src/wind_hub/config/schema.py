"""Pydantic configuration models — system, devices, points, tasks, and top-level Config."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import ConfigError

# ---------------------------------------------------------------------------
# system.yaml
# ---------------------------------------------------------------------------


class RuntimeConfig(BaseModel):
    """Engine runtime parameters (queueing, back-pressure, timeouts)."""

    model_config = ConfigDict(extra="forbid")

    queue_maxsize: int = 1000
    """Capacity of each Sink's internal queue.  When full, the
    runtime applies back-pressure."""

    backpressure_policy: str = "drop_old"
    """Behaviour when a Sink queue is full:
    ``'drop_old'`` — discard oldest data to make room (default);
    ``'drop_new'`` — discard new data, keep queue unchanged;
    ``'block'`` — block the collecting task instance until space frees up."""

    shutdown_timeout: float = 30.0
    """Maximum wait time in seconds during graceful shutdown for
    in-flight operations to complete."""

    connect_timeout: float = 10.0
    """Per-device connection timeout in seconds."""

    read_timeout: float = 5.0
    """Per-read timeout in seconds — 应用层对一次批量读的外层兜底
    （协议驱动内部的底层超时仍各自保留，两层职责见 docs/architecture.md）。"""

    write_timeout: float = 5.0
    """Per-write default timeout in seconds — 命令未自带 ``timeout``
    （``Command.timeout <= 0``）时 Dispatcher 使用的默认写超时。"""

    @model_validator(mode="after")
    def _validate_backpressure(self) -> RuntimeConfig:
        allowed = {"drop_old", "drop_new", "block"}
        if self.backpressure_policy not in allowed:
            raise ConfigError(
                f"Invalid backpressure_policy '{self.backpressure_policy}'; "
                f"must be one of {sorted(allowed)}"
            )
        return self


class PipelineConfig(BaseModel):
    """Processor pipeline definition."""

    model_config = ConfigDict(extra="forbid")

    processors: list[str] = Field(default_factory=list)
    """Ordered list of processor names.  Each name must match a
    registered ``ProcessorPort.name``."""


class SinkConfig(BaseModel):
    """Definition of a single data sink."""

    model_config = ConfigDict(extra="forbid")

    name: str
    """Unique sink name, referenced by collection task targets."""

    type: str
    """Sink driver type: ``'kafka'``, ``'file'``, or ``'db'``."""

    enabled: bool = True
    """Whether this sink is active."""

    params: dict[str, Any] = Field(default_factory=dict)
    """Driver-specific parameters (broker address, file path, DSN, …).
    Validated by the adapter at creation time, not here."""


class ApiConfig(BaseModel):
    """REST API server settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080


class CliConfig(BaseModel):
    """CLI settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class InterfaceConfig(BaseModel):
    """Inbound adapter settings."""

    model_config = ConfigDict(extra="forbid")

    api: ApiConfig = Field(default_factory=ApiConfig)
    cli: CliConfig = Field(default_factory=CliConfig)


class SystemConfig(BaseModel):
    """Top-level system configuration (``system.yaml``)."""

    model_config = ConfigDict(extra="forbid")

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    sinks: list[SinkConfig] = Field(default_factory=list)
    interfaces: InterfaceConfig = Field(default_factory=InterfaceConfig)

    @model_validator(mode="after")
    def _validate_sinks(self) -> SystemConfig:
        sink_names = [s.name for s in self.sinks]
        if len(sink_names) != len(set(sink_names)):
            raise ConfigError(f"Duplicate sink names: {sink_names}")
        return self


# ---------------------------------------------------------------------------
# devices.yaml
# ---------------------------------------------------------------------------


class DeviceConfig(BaseModel):
    """Static configuration of a single device.

    设备只描述「身份、协议、连接、点表绑定」——「什么时候采、采哪些点、
    发到哪些 sink」全部由 ``tasks.yaml`` 的采集 Task 决定。
    """

    model_config = ConfigDict(extra="forbid")

    device_id: str
    protocol: str
    endpoint: Endpoint
    point_table: str
    """绑定的点表名（``points.yaml`` 中 ``point_tables`` 的键）。同类型
    设备共享同一份点表定义，不逐设备复制。"""
    device_group: str | None = None
    """设备业务类别（如 ``'turbine'`` / ``'pcs'`` / ``'substation'`` /
    ``'met_mast'``）——同类大量设备共享同一值，采集 Task 按此维度选择
    设备范围。"""
    enabled: bool = True
    read_mode: str = "sum"
    """ADS 读取策略：``'sum'``（单条 Sum 命令，Symbol 批量寻址，用于周期
    采集）或 ``'sequential'``（逐点 Read，仅用于 CLI/API 单次读取与诊断，
    不参与周期采集）。仅对 ``protocol == 'ads'`` 有意义。"""

    @property
    def supports_scheduled_collection(self) -> bool:
        """是否参与周期采集。

        ADS ``sequential`` 设备只允许请求驱动的单次读取（CLI/API/诊断）——
        配置加载阶段已禁止任何采集 Task 引用此类设备。
        """
        return not (self.protocol == "ads" and self.read_mode == "sequential")

    @model_validator(mode="after")
    def _validate_acquisition(self) -> DeviceConfig:
        if self.read_mode not in ("sum", "sequential"):
            raise ConfigError(
                f"Device '{self.device_id}': read_mode must be 'sum' or 'sequential', "
                f"got '{self.read_mode}'"
            )
        return self


class DevicesConfig(BaseModel):
    """Top-level devices configuration (``devices.yaml``)."""

    model_config = ConfigDict(extra="forbid")

    devices: list[DeviceConfig]

    @model_validator(mode="after")
    def _validate_devices(self) -> DevicesConfig:
        allowed = {"ads", "modbus", "iec104"}
        seen: set[str] = set()
        for d in self.devices:
            if d.protocol not in allowed:
                raise ConfigError(
                    f"Device '{d.device_id}': protocol '{d.protocol}' " f"must be one of {allowed}"
                )
            if d.device_id in seen:
                raise ConfigError(f"Duplicate device_id: '{d.device_id}'")
            seen.add(d.device_id)
        return self


# ---------------------------------------------------------------------------
# points.yaml — 可复用点表（point_tables）
# ---------------------------------------------------------------------------

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

NUMERIC_DATA_TYPES = frozenset(
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
    }
)


class PointAddress(BaseModel):
    """Protocol-specific point address.

    Because address structures differ wildly across protocols (ADS
    uses index_group + index_offset; Modbus uses register type +
    address; IEC104 uses IOA + ASDU type), this model accepts
    arbitrary extra fields.  The protocol adapter interprets them at
    ``connect`` time.
    """

    model_config = ConfigDict(extra="allow")

    type: str | None = None
    """Optional data-type override (e.g. ``'real32'`` for ADS,
    ``'holding_register'`` for Modbus)."""


class PointConfig(BaseModel):
    """Definition of a single measurement point.

    点是**设备无关**的：不携带 ``device_id``，设备通过绑定点表获得点集
    （见 :class:`DeviceConfig.point_table`）。三个标识各司其职、不得混用：

    - ``point_id`` — 系统内部稳定 ID（如 ``p001``），与具体设备无关；
    - ``variable_name`` — 业务变量名（如 ``rotor_speed``），仅供展示与诊断；
    - ``address.symbol`` — PLC Symbol（协议寻址，见 address extra 字段）。
    """

    model_config = ConfigDict(extra="forbid")

    point_id: str
    variable_name: str | None = None
    """业务变量名（展示/诊断用）；``None`` 表示未命名。"""
    point_groups: list[str]
    """点位归属的采集分组集合（多值，至少一个且不重复）——采集 Task 经
    ``point_group`` 单值选点：``task.point_group in point.point_groups``。
    同一个点可属于多个分组、被多个不同 Task 采集。"""
    address: PointAddress
    data_type: str = "float32"
    scale: float = 1.0
    offset: float = 0.0
    unit: str | None = None
    description: str | None = None
    deadband: float | None = None
    """死区阈值（绝对值）。两次输出之差的绝对值小于该值时不再输出该点；
    为 ``None`` 时不过滤。仅对数值类型 ``data_type`` 生效。"""
    min_value: float | None = None
    """质量校验下限（含）。低于该值的读数 quality 标记为 BAD；
    为 ``None`` 时不校验下限。"""
    max_value: float | None = None
    """质量校验上限（含）。高于该值的读数 quality 标记为 BAD；
    为 ``None`` 时不校验上限。"""

    @model_validator(mode="after")
    def _validate_processing_bounds(self) -> PointConfig:
        """校验分组与处理参数边界（处理参数仅对数值类型 data_type 生效）。"""
        if not self.point_groups:
            raise ConfigError(f"Point '{self.point_id}': point_groups must be non-empty")
        if len(self.point_groups) != len(set(self.point_groups)):
            raise ConfigError(
                f"Point '{self.point_id}': duplicate point_groups: {self.point_groups}"
            )
        if any(not g.strip() for g in self.point_groups):
            raise ConfigError(f"Point '{self.point_id}': point_groups must be non-empty strings")
        if self.data_type not in NUMERIC_DATA_TYPES:
            return self
        if self.deadband is not None and self.deadband < 0:
            raise ConfigError(
                f"Point '{self.point_id}': deadband must be >= 0, got {self.deadband}"
            )
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value >= self.max_value
        ):
            raise ConfigError(
                f"Point '{self.point_id}': "
                f"min_value ({self.min_value}) must be < max_value ({self.max_value})"
            )
        return self


class PointPatch(BaseModel):
    """点表继承中的点补丁——子表对父表点的**部分**覆盖或新增点的定义。

    与 :class:`PointConfig` 的区别：除 ``point_id`` 外全部字段可选，
    「未写」与「显式 null」语义不同——merge 以 ``model_fields_set``
    判断字段是否出现在 Raw YAML 中：

    - 未写（不在 ``model_fields_set``）→ 继承父表值；
    - 写了（含显式 ``null``）→ 覆盖父表值。

    本模型**不做**完整点校验（``data_type`` 白名单、deadband、min/max、
    地址约束等）——这些在继承展开为完整 :class:`PointConfig` 后统一执行。
    """

    model_config = ConfigDict(extra="forbid")

    point_id: str
    variable_name: str | None = None
    point_groups: list[str] | None = None
    """显式配置时**整体替换**父表 point_groups（不做 append）。"""
    address: PointAddress | None = None
    """显式配置时**整体替换**父表 address（不做递归深度 merge）。"""
    data_type: str | None = None
    scale: float | None = None
    offset: float | None = None
    unit: str | None = None
    description: str | None = None
    deadband: float | None = None
    min_value: float | None = None
    max_value: float | None = None

    @model_validator(mode="after")
    def _validate_patch(self) -> PointPatch:
        if not self.point_id:
            raise ConfigError("Point patch: point_id must be non-empty")
        return self


class PointTableConfig(BaseModel):
    """Raw 点表（``points.yaml`` 中的表定义）——可复用、可单继承。

    - ``extends``：父表名（单继承，允许多级链）；``None`` 表示基础表；
    - ``remove_points``：按 ``point_id`` 从父表解析结果中删除；
    - ``points``：补丁列表——``point_id`` 已存在于父表结果为 override，
      不存在为新增。

    解析顺序：父表解析结果 → ``remove_points`` → 本表 points
    override/append（见 ``config/point_table_resolver.py``）。
    """

    model_config = ConfigDict(extra="forbid")

    extends: str | None = None
    remove_points: list[str] = Field(default_factory=list)
    points: list[PointPatch] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_raw(self) -> PointTableConfig:
        seen: set[str] = set()
        for p in self.points:
            if p.point_id in seen:
                raise ConfigError(f"Duplicate point_id in table: '{p.point_id}'")
            seen.add(p.point_id)
        if len(self.remove_points) != len(set(self.remove_points)):
            raise ConfigError(f"Duplicate remove_points: {self.remove_points}")
        return self


class ResolvedPointTable(BaseModel):
    """继承展开后的完整点表——同类型设备共享的点集定义（运行模型）。

    Runtime / 协议驱动只接触本模型，不接触 :class:`PointPatch`。
    表内 ``point_id`` 唯一；不同表之间允许重复
    （命名空间相互独立）。
    """

    model_config = ConfigDict(extra="forbid")

    points: list[PointConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_points(self) -> ResolvedPointTable:
        seen: set[str] = set()
        for p in self.points:
            if p.data_type not in ALLOWED_DATA_TYPES:
                raise ConfigError(f"Point '{p.point_id}': unknown data_type '{p.data_type}'")
            if p.point_id in seen:
                raise ConfigError(f"Duplicate point_id in table: '{p.point_id}'")
            seen.add(p.point_id)
        return self


class PointTablesConfig(BaseModel):
    """Top-level points configuration (``points.yaml``)——全部命名 Raw 点表。

    仅作为 ``points.yaml`` 的解析目标与继承解析的输入；运行链路使用
    :class:`ResolvedPointTables`。
    """

    model_config = ConfigDict(extra="forbid")

    tables: dict[str, PointTableConfig] = Field(default_factory=dict)
    """``{点表名: Raw 点表}``。"""


class ResolvedPointTables(BaseModel):
    """继承解析完成后的全部命名点表（运行模型）。

    ``Config.point_tables`` 持有本类型：父表变更在 diff 时体现为全部
    受影响子孙表的 resolved 内容变化，热重载据此精确重注入设备点映射。
    """

    model_config = ConfigDict(extra="forbid")

    tables: dict[str, ResolvedPointTable] = Field(default_factory=dict)
    """``{点表名: 解析后点表}``；设备经 ``DeviceConfig.point_table`` 引用。"""


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
    - ``interval`` 为本 Task 两轮采集之间的等待时间（秒，> 0）——
      语义是「本轮 collect 完成 → 等待 interval → 下一轮」，不是严格
      墙钟周期；
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
    interval: float
    """两轮采集之间的等待时间（秒，必须 > 0）。"""
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
        if self.interval <= 0:
            raise ConfigError(f"Task '{self.task_id}': interval must be > 0, got {self.interval}")
        if not self.targets:
            raise ConfigError(f"Task '{self.task_id}': targets must be non-empty")
        sink_names = [t.sink for t in self.targets]
        if len(sink_names) != len(set(sink_names)):
            raise ConfigError(f"Task '{self.task_id}': duplicate target sinks: {sink_names}")
        return self


class TasksConfig(BaseModel):
    """Top-level tasks configuration (``tasks.yaml``)."""

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
    """One point exposed to the dispatch master via the IEC104 slave proxy.

    Maps a ``(device_id, point_id)`` pair — the engine's internal identity
    of a collected point — to an IEC104 information-object address (IOA)
    and the monitor-direction ASDU type used to report its value.
    """

    model_config = ConfigDict(extra="forbid")

    device_id: str
    """Device identifier, matching ``points.yaml``."""

    point_id: str
    """Point identifier, matching ``points.yaml``."""

    ioa: int
    """Information-object address (0 .. 0xFFFFFF)."""

    data_type: str
    """Monitor-direction ASDU type, e.g. ``'M_ME_NC_1'``."""

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
    """Top-level IEC104 slave proxy configuration (``reporting.yaml``)."""

    model_config = ConfigDict(extra="forbid")

    reporting: list[ReportingPoint] = Field(default_factory=list)
    """Points to expose, in an arbitrary stable order."""

    batch_size: int = 50
    """Soft cap on information objects per interrogation data batch.  The
    hard bound is the 253-byte APDU limit, enforced by the slave handlers
    per ASDU type; this cap only keeps small-object types from emitting an
    excessive number of objects in a single ASDU."""

    common_address: int = 1
    """IEC104 common address (station address, 0 .. 65535)."""

    host: str = "127.0.0.1"
    """Listen host for the slave proxy TCP server."""

    port: int = 12404
    """Listen port for the slave proxy TCP server."""

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
# top-level aggregate
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """Fully-loaded wind-hub configuration.

    This is the single configuration object passed to the engine and
    all adapters at startup.
    """

    model_config = ConfigDict(extra="forbid")

    system: SystemConfig
    devices: DevicesConfig
    point_tables: ResolvedPointTables
    """继承解析完成后的点表集——运行链路只使用 resolved 模型。"""
    tasks: TasksConfig
    """周期采集 Task 定义集——没有 Task 就不进行周期采集。"""
    reporting: ReportingConfig | None = None
    """Optional IEC104 slave proxy config; ``None`` disables the proxy."""

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
