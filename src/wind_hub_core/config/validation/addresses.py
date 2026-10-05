"""协议点地址形式校验。

按点表 protocol 校验全部 resolved 点的地址形式（配置期失败，不等运行时读
失败）；地址动态字段的解释与驱动 ``parse_point`` 保持一致，但不反向依赖
adapter 层。
"""

from __future__ import annotations

from wind_hub_core.config.model.point import PointConfig, ResolvedPointTables
from wind_hub_core.model.errors import ConfigError


def validate_table_addresses(point_tables: ResolvedPointTables) -> None:
    """按点表 protocol 校验全部 resolved 点的地址形式（配置期失败，不等
    运行时读失败）。"""
    for table_name, table in point_tables.tables.items():
        for p in table.points:
            if table.protocol == "ads":
                _validate_ads_address(table_name, p)
            elif table.protocol == "modbus":
                _validate_modbus_address(table_name, p)
            elif table.protocol == "iec104":
                _validate_iec104_address(table_name, p)


def _validate_ads_address(table: str, point: PointConfig) -> None:
    """校验 ADS 点位地址形式。

    合法形式：
      1. 仅 ``symbol``——Symbol 寻址（推荐）；
      2. ``index_group`` + ``index_offset`` 成对——兼容寻址；
      3. 两者同时存在——允许，实际读写以 symbol 优先。

    Raises:
        ConfigError: index 字段不成对，或三者全空。
    """
    extra = point.address.model_extra or {}
    symbol = extra.get("symbol")
    index_group = extra.get("index_group")
    index_offset = extra.get("index_offset")

    if (index_group is None) != (index_offset is None):
        raise ConfigError(
            f"ADS point '{point.point_id}' (table '{table}'): "
            f"'index_group' and 'index_offset' must be configured together"
        )
    if symbol is None and index_group is None:
        raise ConfigError(
            f"ADS point '{point.point_id}' (table '{table}'): address must "
            f"define 'symbol' or 'index_group' + 'index_offset'"
        )


# Modbus register_type 合法取值（与 ``adapter/outbound/protocol/modbus/
# mapping.py`` 的驱动解析保持一致；loader 不反向依赖 adapter 层）。
_MODBUS_REGISTER_TYPES = frozenset(
    {
        "coil",
        "discrete_input",
        "discrete",
        "input",
        "input_register",
        "holding",
        "holding_register",
    }
)


def _validate_modbus_address(table: str, point: PointConfig) -> None:
    """校验 Modbus 点位地址形式（与驱动 ``parse_point`` 的必填字段一致）。

    - ``register_type``（extra 字段）或 ``type``（address 模型字段）必须
      是 coil / discrete_input / holding / input 之一（含别名）；
    - ``address`` 必须是非负整数（0-based 寄存器/线圈偏移）。

    Raises:
        ConfigError: register_type 缺失/非法，或 address 缺失/非法。
    """
    extra = point.address.model_extra or {}
    register_type = extra.get("register_type")
    if register_type is None:
        register_type = point.address.type
    if register_type is None or str(register_type).lower() not in _MODBUS_REGISTER_TYPES:
        raise ConfigError(
            f"Modbus point '{point.point_id}' (table '{table}'): missing or invalid "
            f"register_type '{register_type}' (expected coil/discrete_input/holding/input)"
        )
    address = extra.get("address")
    if isinstance(address, bool) or not isinstance(address, int) or address < 0:
        raise ConfigError(
            f"Modbus point '{point.point_id}' (table '{table}'): address must be "
            f"a non-negative integer, got {address!r}"
        )


def _validate_iec104_address(table: str, point: PointConfig) -> None:
    """校验 IEC104 点位地址形式——``ioa`` 必须是 [0, 0xFFFFFF] 的整数。

    Raises:
        ConfigError: ioa 缺失或越界。
    """
    extra = point.address.model_extra or {}
    ioa = extra.get("ioa")
    if isinstance(ioa, bool) or not isinstance(ioa, int) or not 0 <= ioa <= 0xFFFFFF:
        raise ConfigError(
            f"IEC104 point '{point.point_id}' (table '{table}'): ioa must be "
            f"an integer in [0, 0xFFFFFF], got {ioa!r}"
        )


__all__ = ["validate_table_addresses"]
