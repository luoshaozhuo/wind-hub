"""设备领域配置模型。

覆盖 ``device_models.yaml``（设备类型 / 设备型号）与 ``devices.yaml``（现场
设备实例）的 Raw 模型，以及型号 + 实例合并后的 resolved 运行时
:class:`DeviceConfig`。合并过程由 ``config/resolver/device.py`` 完成。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wind_hub_core.config.model.protocol import SUPPORTED_PROTOCOLS
from wind_hub_core.model.device import Endpoint
from wind_hub_core.model.errors import ConfigError

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
    """Raw YAML root model——``device_models.yaml`` 顶层设备类型与型号定义。"""

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
    :class:`DeviceConfig` 由 ``config/resolver/device.py`` 完成。
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    device_id: str
    model: str
    """设备型号 ID（``device_models.yaml`` 中 ``device_models`` 的键）。"""
    device_group: str | None = None
    endpoint: InstanceEndpoint
    enabled: bool = True


class DeviceInstancesConfig(BaseModel):
    """Raw YAML root model——``devices.yaml`` 的解析目标（文件级 wrapper）。

    仅用于文件解析与 resolver 输入；最终 resolved ``Config.devices`` 是
    扁平的 ``dict[device_id, DeviceConfig]``，不经过本类型。
    """

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
    采集）或 ``'sequential'``（逐点 Read，仅用于 Commander 请求式读取与诊断，
    不参与周期采集）。仅对 ``protocol == 'ads'`` 有意义。"""

    @property
    def supports_scheduled_collection(self) -> bool:
        """是否参与周期采集。

        ADS ``sequential`` 设备只允许 Commander 请求驱动的单次读取与诊断——
        配置加载阶段已禁止任何采集 Task 引用此类设备。
        """
        return not (self.protocol == "ads" and self.read_mode == "sequential")

    @model_validator(mode="after")
    def _validate_acquisition(self) -> DeviceConfig:
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ConfigError(
                f"Device '{self.device_id}': protocol '{self.protocol}' must be one of "
                f"{sorted(SUPPORTED_PROTOCOLS)}"
            )
        if self.read_mode not in ("sum", "sequential"):
            raise ConfigError(
                f"Device '{self.device_id}': read_mode must be 'sum' or 'sequential', "
                f"got '{self.read_mode}'"
            )
        return self


__all__ = [
    "ADS_READ_MODES",
    "DeviceTypeConfig",
    "DeviceModelConfig",
    "DeviceModelsConfig",
    "InstanceEndpoint",
    "DeviceInstanceConfig",
    "DeviceInstancesConfig",
    "DeviceConfig",
]
