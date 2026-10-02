"""把 IEC104 reporting 配置编译为运行时查找表。

本模块是纯转换逻辑，不执行 I/O。生成的字典分别服务于快照更新、监视方向
ASDU 组装和控制方向命令寻址。
"""

from __future__ import annotations

from wind_hub.config.schema import ReportingPoint


def build_ioa_mapping(reporting: list[ReportingPoint]) -> dict[tuple[str, str], int]:
    """构建 (device_id, point_id) 到 IOA 的映射。

    Args:
        reporting: 已通过配置校验的 reporting 点列表。

    Returns:
        采集点身份到 IEC104 IOA 的字典。
    """
    return {(p.device_id, p.point_id): p.ioa for p in reporting}


def build_data_type_mapping(reporting: list[ReportingPoint]) -> dict[int, str]:
    """构建 IOA 到监视方向 ASDU TypeID 名称的映射。"""
    return {p.ioa: p.data_type for p in reporting}


def build_reverse_mapping(reporting: list[ReportingPoint]) -> dict[int, tuple[str, str]]:
    """构建 IOA 到 (device_id, point_id) 的控制寻址反向映射。"""
    return {p.ioa: (p.device_id, p.point_id) for p in reporting}
