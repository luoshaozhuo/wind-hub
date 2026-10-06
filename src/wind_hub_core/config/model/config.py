"""顶层聚合配置 :class:`Config`——resolved 配置快照。

组合根和 Runtime 只消费本对象，不再读取原始 YAML；各子领域模型见
``config/model/`` 的 system / unit / device / point / task / sink。

本对象是**不可变配置快照**：字段禁止重新赋值（``frozen=True``）；内部
collection 依靠架构纪律禁止原地修改——配置更新必须构造新的 Config
快照并整体替换（见 ``config/loader.py`` 与 ``config/diff.py``）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wind_hub_core.config.model.device import (
    DeviceConfig,
    DeviceModelConfig,
    DeviceTypeConfig,
)
from wind_hub_core.config.model.point import PointConfig, ResolvedPointTable
from wind_hub_core.config.model.sink import ResolvedSinkConfig
from wind_hub_core.config.model.system import SystemConfig
from wind_hub_core.config.model.task import CollectionTaskConfig
from wind_hub_core.config.model.unit import UnitConfig
from wind_hub_core.model.errors import ConfigError


class Config(BaseModel):
    """完成加载、继承展开和跨文件校验后的 resolved 配置快照。

    全部集合都是按稳定业务身份索引的扁平 dict——YAML 文件级 wrapper
    （``DeviceInstancesConfig`` / ``TasksConfig`` / ``SinksConfig`` 等）
    只存在于 loader/resolver 阶段，不进入本快照。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    system: SystemConfig
    devices: dict[str, DeviceConfig] = Field(default_factory=dict)
    """resolved 设备索引——``{device_id: DeviceConfig}``。"""
    sinks: dict[str, ResolvedSinkConfig] = Field(default_factory=dict)
    """resolved Sink 索引——``{sink.name: ResolvedSinkConfig}``；
    sinks.yaml 的统一 Sink 定义，是 Runtime 与外部接口契约的唯一来源。"""
    tasks: dict[str, CollectionTaskConfig] = Field(default_factory=dict)
    """resolved 采集 Task 索引——``{task_id: CollectionTaskConfig}``；
    没有 Task 就不进行周期采集。"""
    point_tables: dict[str, ResolvedPointTable] = Field(default_factory=dict)
    """继承解析完成后的点表索引——``{点表名: ResolvedPointTable}``；
    运行链路只使用 resolved 模型。"""
    units: dict[str, UnitConfig] = Field(default_factory=dict)
    """单位索引——``{unit_id: UnitConfig}``；``PointConfig.unit`` 引用的
    unit ID 命名空间，展示层经 ``units[unit_id].symbol`` 取显示符号。"""
    device_types: dict[str, DeviceTypeConfig] = Field(default_factory=dict)
    """公共设备类型定义（``device_models.yaml``）——仅业务分类元数据。"""
    device_models: dict[str, DeviceModelConfig] = Field(default_factory=dict)
    """公共设备型号定义——运行链路不直接消费（设备已 resolve 为
    :class:`~wind_hub_core.config.model.device.DeviceConfig`），保留用于
    diff、诊断与导出。"""

    @model_validator(mode="after")
    def _validate_key_identity(self) -> Config:
        """确保 dict 键与对象内部身份字段一致。

        对象会脱离 Config 独立传递，必须保持自描述——键只是索引，身份
        以对象字段为准。
        """
        for key, device in self.devices.items():
            if key != device.device_id:
                raise ConfigError(
                    f"devices: key '{key}' does not match device_id '{device.device_id}'"
                )
        for key, sink in self.sinks.items():
            if key != sink.name:
                raise ConfigError(f"sinks: key '{key}' does not match sink name '{sink.name}'")
        for key, task in self.tasks.items():
            if key != task.task_id:
                raise ConfigError(
                    f"tasks: key '{key}' does not match task_id '{task.task_id}'"
                )
        return self

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
        device = self.devices[device_id]
        return list(self.point_tables[device.point_table].points)

    def points_by_device(self) -> dict[str, list[PointConfig]]:
        """``{device_id: 点集}``——每个键都是独立 list（见
        :meth:`points_for_device` 的快拍语义）。"""
        return {
            device_id: self.points_for_device(device_id) for device_id in self.devices
        }


__all__ = [
    "Config",
]
