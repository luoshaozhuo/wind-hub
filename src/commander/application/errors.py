"""Commander Application 层稳定异常。"""

from __future__ import annotations


class CommandError(Exception):
    """即时设备操作的用例级失败（未知设备/点、不可写、未连接等）。

    gRPC inbound adapter 将本异常映射为契约错误码；配置类错误统一使用
    ``core.application.ConfigError``。
    """
