"""Pydantic configuration models — system, devices, points, routing, and top-level Config."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wind_hub.domain.model.device import Endpoint
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.model.route import RouteRule

# ---------------------------------------------------------------------------
# system.yaml
# ---------------------------------------------------------------------------


class SchedulerConfig(BaseModel):
    """Engine scheduling parameters."""

    model_config = ConfigDict(extra="forbid")

    default_interval: float = 1.0
    """Default polling interval in seconds when a device has no
    explicit polling group."""

    max_concurrent_devices: int = 32
    """Maximum number of devices polled concurrently."""

    queue_maxsize: int = 1000
    """Capacity of each Sink's internal queue.  When full, the
    scheduler applies back-pressure."""

    backpressure_policy: str = "drop_old"
    """Behaviour when a Sink queue is full:
    ``'drop_old'`` — discard oldest data to make room (default);
    ``'drop_new'`` — discard new data, keep queue unchanged;
    ``'block'`` — block the polling task until space frees up."""

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
    def _validate_backpressure(self) -> SchedulerConfig:
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
    """Unique sink name, referenced by routing rules and per-point overrides."""

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

    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
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


class PollingGroup(BaseModel):
    """A polling group defines how often a device scans a subset of points."""

    model_config = ConfigDict(extra="forbid")

    group: str
    """Group name (e.g. ``'telemetry'``, ``'signals'``)."""

    interval: float
    """Polling interval for this group in seconds."""


class SubscribeConfig(BaseModel):
    """Device-notification (push) parameters for a device.

    Only meaningful when the protocol driver supports spontaneous
    updates (e.g. ADS device notifications).  Values are consumed by
    the adapter, which owns the connection-pool sizing.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    """Whether to subscribe to spontaneous updates instead of (or in
    addition to) polling."""

    cycle_time: float = 0.02
    """Notification cycle time in seconds (minimum update period)."""

    max_delay: float = 0.06
    """Maximum notification delay in seconds."""

    max_notifications_per_connection: int = 550
    """Notification-handle budget per connection; the adapter opens an
    additional connection once a connection's handles exceed this."""


class DeviceConfig(BaseModel):
    """Static configuration of a single device."""

    model_config = ConfigDict(extra="forbid")

    device_id: str
    protocol: str
    endpoint: Endpoint
    point_table: str
    """绑定的点表名（``points.yaml`` 中 ``point_tables`` 的键）。同类型
    设备共享同一份点表定义，不逐设备复制。"""
    polling: list[PollingGroup] = Field(default_factory=list)
    enabled: bool = True
    read_mode: str = "sum"
    """ADS 读取策略：``'sum'``（单条 Sum 命令，Symbol 批量寻址，用于周期
    采集）或 ``'sequential'``（逐点 Read，用于 CLI/API 单次读取与诊断）。
    仅对 ``protocol == 'ads'`` 有意义。"""

    mode: str = "poll"
    """采集模式：``'poll'``（调度器周期轮询）或 ``'subscribe'``（订阅推送，
    不轮询）。不支持两者混合。"""

    subscribe: SubscribeConfig = Field(default_factory=SubscribeConfig)
    """Push-subscription settings; see :class:`SubscribeConfig`."""

    @model_validator(mode="after")
    def _validate_acquisition(self) -> DeviceConfig:
        if self.read_mode not in ("sum", "sequential"):
            raise ConfigError(
                f"Device '{self.device_id}': read_mode must be 'sum' or 'sequential', "
                f"got '{self.read_mode}'"
            )
        if self.mode not in ("poll", "subscribe"):
            raise ConfigError(
                f"Device '{self.device_id}': mode must be 'poll' or 'subscribe', "
                f"got '{self.mode}'"
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
    - ``name`` — 业务变量名（如 ``rotor_speed``），仅供展示与诊断；
    - ``address.symbol`` — PLC Symbol（协议寻址，见 address extra 字段）。
    """

    model_config = ConfigDict(extra="forbid")

    point_id: str
    name: str | None = None
    """业务变量名（展示/诊断用）；``None`` 表示未命名。"""
    group: str = "default"
    """采集分组——设备 ``polling`` 配置按组定义周期；点通过本字段归类，
    一个 ``(device, group)`` 对应一个调度 Job。"""
    address: PointAddress
    data_type: str = "float32"
    scale: float = 1.0
    offset: float = 0.0
    unit: str | None = None
    description: str | None = None
    sinks: list[str] | None = None
    """Optional per-point routing override.  When set, this list
    replaces the default routing rules for this point."""
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
        """校验处理参数边界（仅对数值类型 data_type 生效）。"""
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


class PointTableConfig(BaseModel):
    """一份可复用点表——同类型设备共享的完整点集定义。

    表内 ``point_id`` 唯一；不同表之间允许重复（命名空间相互独立）。
    """

    model_config = ConfigDict(extra="forbid")

    points: list[PointConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_points(self) -> PointTableConfig:
        seen: set[str] = set()
        for p in self.points:
            if p.data_type not in ALLOWED_DATA_TYPES:
                raise ConfigError(
                    f"Point '{p.point_id}': unknown data_type '{p.data_type}'"
                )
            if p.point_id in seen:
                raise ConfigError(f"Duplicate point_id in table: '{p.point_id}'")
            seen.add(p.point_id)
        return self


class PointTablesConfig(BaseModel):
    """Top-level points configuration (``points.yaml``)——全部命名点表。"""

    model_config = ConfigDict(extra="forbid")

    tables: dict[str, PointTableConfig] = Field(default_factory=dict)
    """``{点表名: 点表}``；设备经 ``DeviceConfig.point_table`` 引用。"""


# ---------------------------------------------------------------------------
# routing.yaml
# ---------------------------------------------------------------------------


class RoutingConfig(BaseModel):
    """Top-level routing configuration (``routing.yaml``)."""

    model_config = ConfigDict(extra="forbid")

    rules: list[RouteRule] = Field(default_factory=list)
    """Ordered routing rules — evaluated in descending priority."""

    unmatched_policy: str = "drop"
    """Behaviour for points not covered by any rule:
    ``'drop'`` — silently discard (default);
    ``'error'`` — fail at startup if any point is unmatched;
    ``'default'`` — reserved for future implementation."""

    @model_validator(mode="after")
    def _validate_rules(self) -> RoutingConfig:
        names = [r.name for r in self.rules]
        if len(names) != len(set(names)):
            raise ConfigError(f"Duplicate routing rule names: {names}")
        allowed = {"drop", "error", "default"}
        if self.unmatched_policy not in allowed:
            raise ConfigError(
                f"Invalid unmatched_policy '{self.unmatched_policy}'; "
                f"must be one of {sorted(allowed)}"
            )
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
    point_tables: PointTablesConfig
    routing: RoutingConfig
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
