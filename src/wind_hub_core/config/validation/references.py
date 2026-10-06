"""跨文件引用一致性校验。

设备型号 ↔ 点表、点 ↔ 单位、设备 ↔ 点表绑定的引用完整性校验；输入全部是
resolved 配置模型，校验失败一律抛 :class:`ConfigError`。
"""

from __future__ import annotations

from wind_hub_core.config.model.device import DeviceConfig, DeviceModelsConfig
from wind_hub_core.config.model.point import ResolvedPointTable
from wind_hub_core.config.model.unit import UnitsConfig
from wind_hub_core.model.errors import ConfigError


def validate_model_point_tables(
    device_models: DeviceModelsConfig,
    point_tables: dict[str, ResolvedPointTable],
) -> None:
    """校验全部型号引用的点表存在、且型号协议与点表协议一致（未被实例
    引用的型号同样校验——型号定义自身必须自洽）。"""
    for model_id, m in device_models.device_models.items():
        table = point_tables.get(m.point_table)
        if table is None:
            raise ConfigError(
                f"Device model '{model_id}' references unknown point_table "
                f"'{m.point_table}' (available: {sorted(point_tables)})"
            )
        if m.protocol != table.protocol:
            raise ConfigError(
                f"Device model '{model_id}': protocol '{m.protocol}' does not match "
                f"point table '{m.point_table}' protocol '{table.protocol}'"
            )


def validate_point_units(
    point_tables: dict[str, ResolvedPointTable],
    units: UnitsConfig,
) -> None:
    """校验继承展开后全部点的 ``unit`` 是已定义的 unit ID。"""
    for table_name, table in point_tables.items():
        for p in table.points:
            if p.unit not in units.units:
                raise ConfigError(
                    f"Point '{p.point_id}' (table '{table_name}') references "
                    f"unknown unit '{p.unit}' (available: {sorted(units.units)})"
                )


def validate_device_binding(
    device: DeviceConfig,
    point_tables: dict[str, ResolvedPointTable],
) -> None:
    """校验单台设备的点表绑定与 read_mode 组合约束。

    点地址形式已在 ``validation/addresses.py`` 按点表 protocol 统一
    校验；此处只保留设备级约束（ADS ``sum`` 的全 symbol 要求）。

    Raises:
        ConfigError: 点表缺失或 read_mode 组合非法。
    """
    table = point_tables.get(device.point_table)
    if table is None:
        raise ConfigError(
            f"Device '{device.device_id}' references unknown point_table "
            f"'{device.point_table}' (available: {sorted(point_tables)})"
        )

    if device.protocol == "ads" and device.read_mode == "sum":
        for p in table.points:
            # sum 模式按 symbol 批量读，绑定表必须全部 symbol 寻址
            symbol = (p.address.model_extra or {}).get("symbol")
            if symbol is None:
                raise ConfigError(
                    f"ADS point '{p.point_id}' (device '{device.device_id}'): "
                    f"read_mode='sum' requires 'symbol' on every point of "
                    f"table '{device.point_table}'"
                )


__all__ = [
    "validate_model_point_tables",
    "validate_point_units",
    "validate_device_binding",
]
