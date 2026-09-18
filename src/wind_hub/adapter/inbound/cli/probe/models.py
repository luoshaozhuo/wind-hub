"""probe discover 的数据模型——纯逻辑、无 I/O，可独立单测。

:class:`DiscoveredPoint` 是「发现到的一个点」的统一表示：ADS 符号浏览
得到真实符号，Modbus 寄存器扫描得到合成标签（如 ``holding[100]``）。
:func:`symbol_to_point_id` 把符号名转换成 points.yaml 风格的 point_id。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# point_id 中允许保留的字符之外的字符统一替换为下划线（决策 5）：
# ``\w`` 在 Unicode 模式下涵盖中英文、数字与下划线，点号单独保留。
_NON_POINT_ID_CHAR_RE = re.compile(r"[^\w.]")

# 默认剥离的 PLC 程序前缀（决策 5）。TwinCAT 项目的全局变量惯例挂在
# ``MAIN.`` 程序组织单元下，point_id 不需要携带这一层。
_MAIN_PREFIX = "MAIN."


@dataclass(frozen=True)
class DiscoveredPoint:
    """发现的一个点。

    Attributes:
        symbol: 符号名（ADS，如 ``"MAIN.风机1.转速"``）或合成标签
            （Modbus 扫描，如 ``"holding[100]"``）。
        data_type: 推导的 data_type（如 ``"float32"``；无法识别时为
            ``"unknown"``，草稿需人工确认）。
        size: 字节数。
        comment: 符号注释（如果协议提供）；Modbus 扫描结果固定为
            「扫描结果，需人工确认」。
        address: 可选的显式地址字典。``None``（默认）表示符号寻址，
            :meth:`to_yaml_dict` 输出 ``{"symbol": symbol}``；Modbus
            扫描结果携带 ``{"register_type": ..., "address": ...}``。
    """

    symbol: str
    data_type: str
    size: int
    comment: str | None = None
    address: dict[str, Any] | None = None

    def to_yaml_dict(self, point_id: str) -> dict[str, Any]:
        """转换为 YAML 草稿中一个点位条目的字典形式。

        结构与 :class:`~wind_hub.config.schema.PointConfig` 兼容
        （``point_id`` / ``address`` / ``data_type`` / ``unit`` /
        ``description``）——点表设备无关，条目不含 ``device_id``。
        """
        return {
            "point_id": point_id,
            "address": self.address if self.address is not None else {"symbol": self.symbol},
            "data_type": self.data_type,
            "unit": None,
            "description": self.comment,
        }


def symbol_to_point_id(symbol: str) -> str:
    """符号名 → point_id 的默认转换（决策 5）。

    规则：

    - 去掉开头的 ``MAIN.`` 前缀（如果存在）；
    - 转小写（对中文无影响——中文按决策 5 直接保留）；
    - 点号和下划线保留；
    - 其他字符替换为下划线；
    - 替换产生的末尾下划线剥离（point_id 不以 ``_`` 结尾，如
      ``holding[100]`` 的 ``]``）。

    Examples:
        >>> symbol_to_point_id("MAIN.风机1.转速")
        '风机1.转速'
        >>> symbol_to_point_id("MAIN.GVL.nWindSpeed")
        'gvl.nwindspeed'
        >>> symbol_to_point_id("holding[100]")
        'holding_100'
    """
    name = symbol
    if name.startswith(_MAIN_PREFIX):
        name = name[len(_MAIN_PREFIX) :]
    return _NON_POINT_ID_CHAR_RE.sub("_", name.lower()).rstrip("_")
