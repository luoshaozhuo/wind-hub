"""Wind Hub 跨进程共享的静态配置模型。

本模块定义设备、点表、单位、现场标识与 ADS 进程级参数等跨 Collector、
Commander、Server 共享的配置语义。这里只做纯 schema 与局部校验，不读取 YAML、
不创建 Runtime，也不包含 Task、Sink、Reporting 等 Collector 专属配置。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from wind_hub_core.model.device import Endpoint
from wind_hub_core.model.errors import ConfigError

def _validate_ams_net_id(net_id: str) -> str:
    """校验六段十进制 AMS Net ID。

    Args:
        net_id: 待校验 AMS Net ID。

    Returns:
        格式和每段范围都合法时返回原值。

    Raises:
        ValueError: 格式非法。
    """
    parts = net_id.split(".")
    if len(parts) != 6 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        raise ConfigError(
            f"Invalid AMS Net ID '{net_id}'; expected dotted-numeric 'a.b.c.d.e.f'"
        )
    return net_id


class ADSSystemConfig(BaseModel):
    """进程级 ADS 本机身份与凭据。"""

    model_config = ConfigDict(extra="forbid")

    local_ams_net_id: str
    """本机 AMS Net ID。"""

    local_ip: str
    """本机 IP。"""

    username: str = "Administrator"
    """ADS 管理用户名；供显式路由/诊断工具使用，不做自动 route repair。"""

    password: str = ""
    """ADS 管理密码，默认空。"""

    @field_validator("local_ams_net_id")
    @classmethod
    def _check_local_ams_net_id(cls, v: str) -> str:
        return _validate_ams_net_id(v)

class SiteConfig(BaseModel):
    """当前部署实例所属现场的身份（``system.yaml`` 的 ``site`` 段）。

    一个 wind-hub 运行实例只对应一个现场；不存在多现场嵌套配置。
    """

    model_config = ConfigDict(extra="forbid")

    site_id: str
    """现场唯一标识（如 ``'wind_farm_a'``）。"""

    name: str | None = None
    """现场显示名（如 ``'某某风电场'``）。"""

    @field_validator("site_id")
    @classmethod
    def _check_site_id(cls, v: str) -> str:
        if not v.strip():
            raise ConfigError("site_id must be non-empty")
        return v


# ---------------------------------------------------------------------------
# units.yaml — 单位定义（配置元数据）
# ---------------------------------------------------------------------------


class UnitConfig(BaseModel):
    """单个单位定义——``units.yaml`` 中 ``units`` 字典的值。

    点（:class:`PointConfig`）经 unit ID（``units`` 的键）引用单位；
    ``symbol`` 是展示层使用的显示符号（``kilowatt → kW``）。
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    """显示符号（如 ``'kW'``）；允许空字符串表示无量纲。"""

    name: str | None = None
    """单位显示名（如 ``'Kilowatt'``）。"""


class UnitsConfig(BaseModel):
    """units.yaml 顶层单位定义。

    unit ID 唯一性由 dict 键自然保证；ID 非空在此校验。
    """

    model_config = ConfigDict(extra="forbid")

    units: dict[str, UnitConfig]

    @model_validator(mode="after")
    def _validate_units(self) -> UnitsConfig:
        for unit_id in self.units:
            if not unit_id.strip():
                raise ConfigError("units: unit ID must be non-empty")
        if "none" not in self.units:
            raise ConfigError("units: must define the 'none' (dimensionless) unit")
        return self


# ---------------------------------------------------------------------------
# device_models.yaml — 设备类型 / 设备型号
# ---------------------------------------------------------------------------

SUPPORTED_PROTOCOLS = frozenset({"ads", "modbus", "iec104"})
"""现有协议驱动支持的协议集合。"""

ADS_READ_MODES = frozenset({"sum", "sequential"})


class DeviceTypeConfig(BaseModel):
    """设备业务类型（如 ``turbine`` / ``pcs`` / ``bms`` / ``met_mast`` /
    ``substation``）——仅承载业务分类语义，字段保持精简。"""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    """类型显示名（如 ``'风力发电机组'``）。"""


class DeviceModelConfig(BaseModel):
    """设备型号——某一设备型号的通用属性与连接默认值（公共产品定义）。

    点表绑定在型号层（``point_table``），同型号全部设备实例共享；实例只
    描述现场差异（``endpoint`` 覆盖 ``connection_defaults``）。
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    device_type: str
    """所属设备类型（``device_types`` 的键）。"""
    manufacturer: str | None = None
    model: str | None = None
    """厂商硬件型号名（如 ``'2MW'``）；与配置键（型号 ID）区分。"""
    protocol: str
    point_table: str
    """绑定的点表名（``points.yaml`` 中 ``point_tables`` 的键）。"""
    read_mode: str | None = None
    """ADS 读取策略（``'sum'`` / ``'sequential'``，缺省 ``'sum'``）。
    仅 ``protocol == 'ads'`` 可配置；其他协议配置此字段是配置错误。"""
    properties: dict[str, Any] = Field(default_factory=dict)
    """型号级开放属性（如 ``rated_power_kw``）——不做强类型约束。"""
    connection_defaults: dict[str, Any] = Field(default_factory=dict)
    """连接默认值——``port`` 并入 Endpoint.port，其余键并入
    ``Endpoint.extensions``；实例 ``endpoint`` 同名字段优先。"""

    @model_validator(mode="after")
    def _validate_model(self) -> DeviceModelConfig:
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ConfigError(
                f"Device model: protocol '{self.protocol}' must be one of "
                f"{sorted(SUPPORTED_PROTOCOLS)}"
            )
        if self.protocol == "ads":
            if self.read_mode is not None and self.read_mode not in ADS_READ_MODES:
                raise ConfigError(
                    f"ADS device model: read_mode must be 'sum' or 'sequential', "
                    f"got '{self.read_mode}'"
                )
        elif self.read_mode is not None:
            raise ConfigError(
                f"Device model (protocol '{self.protocol}'): read_mode is "
                "ADS-specific and must not be configured for other protocols"
            )
        return self


class DeviceModelsConfig(BaseModel):
    """device_models.yaml 顶层设备类型与型号定义。"""

    model_config = ConfigDict(extra="forbid")

    device_types: dict[str, DeviceTypeConfig] = Field(default_factory=dict)
    device_models: dict[str, DeviceModelConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_refs(self) -> DeviceModelsConfig:
        for model_id, m in self.device_models.items():
            if m.device_type not in self.device_types:
                raise ConfigError(
                    f"Device model '{model_id}' references unknown device_type "
                    f"'{m.device_type}' (available: {sorted(self.device_types)})"
                )
        return self


# ---------------------------------------------------------------------------
# devices.yaml — 现场设备实例
# ---------------------------------------------------------------------------


class InstanceEndpoint(BaseModel):
    """设备实例的连接端点——只写现场差异；``port`` 可省略，由型号的
    ``connection_defaults.port`` 提供。"""

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class DeviceInstanceConfig(BaseModel):
    """现场设备实例（``devices.yaml`` 的原始 Schema）。

    实例只描述「身份、型号引用、连接差异」——协议、点表、读取策略、连接
    默认值全部来自 :class:`DeviceModelConfig`；合并为运行时
    :class:`DeviceConfig` 由 ``config/device_resolver.py`` 完成。
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    device_id: str
    model: str
    """设备型号 ID（``device_models.yaml`` 中 ``device_models`` 的键）。"""
    device_group: str | None = None
    endpoint: InstanceEndpoint
    enabled: bool = True


class DeviceInstancesConfig(BaseModel):
    """Top-level device instances configuration（``devices.yaml`` 的解析目标）。"""

    model_config = ConfigDict(extra="forbid")

    devices: list[DeviceInstanceConfig]

    @model_validator(mode="after")
    def _validate_unique(self) -> DeviceInstancesConfig:
        seen: set[str] = set()
        for d in self.devices:
            if d.device_id in seen:
                raise ConfigError(f"Duplicate device_id: '{d.device_id}'")
            seen.add(d.device_id)
        return self


# ---------------------------------------------------------------------------
# resolved DeviceConfig — 运行时设备模型
# ---------------------------------------------------------------------------


class DeviceConfig(BaseModel):
    """Static configuration of a single device（resolved 运行时模型）。

    由 ``DeviceInstanceConfig`` + ``DeviceModelConfig`` 在 Loader 阶段合并
    展开——Runtime / Device / Task / Driver 只接触本模型，不回查原始
    DeviceModel。「什么时候采、采哪些点、发到哪些 sink」全部由
    ``tasks.yaml`` 的采集 Task 决定。
    """

    model_config = ConfigDict(extra="forbid")

    device_id: str
    protocol: str
    endpoint: Endpoint
    point_table: str
    """绑定的点表名（继承自设备型号；``points.yaml`` 中
    ``point_tables`` 的键）。同型号设备共享同一份点表定义。"""
    device_type: str | None = None
    """设备业务类型（继承自设备型号，如 ``'turbine'``）。"""
    model: str | None = None
    """设备型号 ID（继承来源；``device_models.yaml`` 的键）。"""
    device_group: str | None = None
    """设备采集分组——同类大量设备共享同一值，采集 Task 按此维度选择
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
    """devices.yaml 顶层现场设备实例定义。"""

    model_config = ConfigDict(extra="forbid")

    devices: list[DeviceConfig]

    @model_validator(mode="after")
    def _validate_devices(self) -> DevicesConfig:
        seen: set[str] = set()
        for d in self.devices:
            if d.protocol not in SUPPORTED_PROTOCOLS:
                raise ConfigError(
                    f"Device '{d.device_id}': protocol '{d.protocol}' must be one of "
                    f"{sorted(SUPPORTED_PROTOCOLS)}"
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
    """协议特有点地址。

    允许 extra 字段，因为 ADS、Modbus、IEC104 的地址结构不同；这些动态字段由
    Loader 和协议 adapter 按 point table protocol 解释和校验。
    """

    model_config = ConfigDict(extra="allow")

    type: str | None = None
    """可选协议数据类型覆盖字段。"""


class PointConfig(BaseModel):
    """单个设备无关点定义。

    point_id 是系统稳定 ID；variable_name 用于业务展示/诊断；address 保存协议
    寻址字段。设备通过 point_table 绑定获得点集。
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
    """工程值换算系数——``Device`` 对数值读数应用 ``value * scale + offset``。"""
    offset: float = 0.0
    """工程值换算偏移——见 ``scale``。"""
    unit: str = "none"
    """单位 ID——引用 ``units.yaml`` 中 ``units`` 的键（如 ``kilowatt``，
    展示符号 ``kW`` 由单位定义提供）；``'none'`` 表示无量纲。ID 合法性
    由 Loader 在继承展开后统一跨文件校验。"""
    description: str | None = None

    @model_validator(mode="after")
    def _validate_point_groups(self) -> PointConfig:
        if not self.point_groups:
            raise ConfigError(f"Point '{self.point_id}': point_groups must be non-empty")
        if len(self.point_groups) != len(set(self.point_groups)):
            raise ConfigError(
                f"Point '{self.point_id}': duplicate point_groups: {self.point_groups}"
            )
        if any(not g.strip() for g in self.point_groups):
            raise ConfigError(f"Point '{self.point_id}': point_groups must be non-empty strings")
        return self


class PointPatch(BaseModel):
    """点表继承中的点补丁——子表对父表点的**部分**覆盖或新增点的定义。

    与 :class:`PointConfig` 的区别：除 ``point_id`` 外全部字段可选，
    「未写」与「显式 null」语义不同——merge 以 ``model_fields_set``
    判断字段是否出现在 Raw YAML 中：

    - 未写（不在 ``model_fields_set``）→ 继承父表值；
    - 写了（含显式 ``null``）→ 覆盖父表值。

    本模型**不做**完整点校验（``data_type`` 白名单、地址约束等）——这些在
    继承展开为完整 :class:`PointConfig` 后统一执行。
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

    @model_validator(mode="after")
    def _validate_patch(self) -> PointPatch:
        if not self.point_id:
            raise ConfigError("Point patch: point_id must be non-empty")
        return self


class PointTableConfig(BaseModel):
    """Raw 点表（``points.yaml`` 中的表定义）——可复用、可单继承。

    - ``protocol``：点表协议——决定点 ``address`` 的编辑与校验形式。
      基础表（无 ``extends``）必填；子表可省略（继承父表），显式配置时
      必须与父表一致（禁止跨协议继承，由 resolver 校验）；
    - ``extends``：父表名（单继承，允许多级链）；``None`` 表示基础表；
    - ``remove_points``：按 ``point_id`` 从父表解析结果中删除；
    - ``points``：补丁列表——``point_id`` 已存在于父表结果为 override，
      不存在为新增。

    解析顺序：父表解析结果 → ``remove_points`` → 本表 points
    override/append（见 ``config/point_table_resolver.py``）。
    """

    model_config = ConfigDict(extra="forbid")

    protocol: str | None = None
    """点表协议（``ads`` / ``modbus`` / ``iec104``）；基础表必填，子表
    缺省时继承父表。"""
    extends: str | None = None
    remove_points: list[str] = Field(default_factory=list)
    points: list[PointPatch] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_raw(self) -> PointTableConfig:
        if self.protocol is not None and self.protocol not in SUPPORTED_PROTOCOLS:
            raise ConfigError(
                f"Point table: protocol '{self.protocol}' must be one of "
                f"{sorted(SUPPORTED_PROTOCOLS)}"
            )
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

    protocol: str
    """点表协议（继承展开后的最终值）——运行态与 admin 直接读取。"""
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


