"""点与点表领域配置模型。

覆盖 ``points.yaml`` 的 Raw 点表（可单继承的 :class:`PointTableConfig` +
:class:`PointPatch`）与继承展开后的 resolved 点表
（:class:`ResolvedPointTable` / :class:`ResolvedPointTables`）。继承解析由
``config/resolver/point_table.py`` 完成。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wind_hub_core.config.model.protocol import SUPPORTED_PROTOCOLS
from wind_hub_core.model.errors import ConfigError

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
    override/append（见 ``config/resolver/point_table.py``）。
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


__all__ = [
    "ALLOWED_DATA_TYPES",
    "NUMERIC_DATA_TYPES",
    "PointAddress",
    "PointConfig",
    "PointPatch",
    "PointTableConfig",
    "PointTablesConfig",
    "ResolvedPointTable",
    "ResolvedPointTables",
]
