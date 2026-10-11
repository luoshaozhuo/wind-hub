"""Commander Application 层稳定异常。"""

from __future__ import annotations


class CommandError(Exception):
    """即时设备操作的用例级失败（未知设备/点、不可写、未连接等）。

    gRPC inbound adapter 将本异常映射为契约错误码；配置类错误统一使用
    ``core.application.ConfigError``。
    """


class CommandUncertainError(CommandError):
    """命令可能已送达设备但未获得确认证据的执行结果。

    与 ``CommandError``（确证未执行：本地校验失败或设备明确拒绝）
    区分；调用方不得对本结果盲目重试。
    """
