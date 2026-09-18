"""probe discover 的 YAML 草稿输出（决策 4）。

生成的草稿与 ``points.yaml`` 结构兼容（顶层 ``points:`` 列表，条目字
段同 :class:`~wind_hub.config.schema.PointConfig`），但它是**草稿**：
- ADS 符号的 ``data_type`` 可能是 ``unknown``（类型映射表未收录）；
- Modbus 扫描结果的地址、类型都只是「读得通」的证据。
因此文件头固定带「需人工确认」的注释。
"""

from __future__ import annotations

import yaml

from wind_hub.adapter.inbound.cli.probe.models import DiscoveredPoint, symbol_to_point_id

# 草稿文件头注释——固定提示这是生成物、不能直接当配置用。
_HEADER_COMMENT = (
    "# 此文件由 wind-hub probe discover 生成，需人工确认后方可并入 points.yaml\n"
    "# - data_type 为 unknown 的条目请根据 PLC 实际类型修正\n"
)

# Modbus 扫描结果追加的风险提示（决策 8）。
_MODBUS_SCAN_COMMENT = (
    "# - 本文件为寄存器扫描结果：地址仅代表「读得通」，" "寄存器语义与数据类型需人工确认\n"
)


def render_yaml_draft(
    device_id: str,
    points: list[DiscoveredPoint],
    protocol: str,
) -> str:
    """生成与 points.yaml 兼容的 YAML 草稿文本。

    Args:
        device_id: 草稿条目的 ``device_id``（取命令行指定的设备）。
        points: 发现点列表；point_id 由 :func:`symbol_to_point_id` 生成。
        protocol: 协议名；``"modbus"`` 时追加扫描风险提示注释。
    """
    entries = [
        point.to_yaml_dict(device_id=device_id, point_id=symbol_to_point_id(point.symbol))
        for point in points
    ]
    body = yaml.safe_dump(
        {"points": entries},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    header = _HEADER_COMMENT
    if protocol == "modbus":
        header += _MODBUS_SCAN_COMMENT
    return header + body
