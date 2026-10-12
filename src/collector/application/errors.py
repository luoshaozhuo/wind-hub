"""Collector Application 层错误类型。

Sink 写路径的运行期失败（磁盘满、broker 拒收、数据库不可达等）统一抛出
:class:`SinkError`；配置期失败沿用 ``core.application.ConfigError``。
"""

from __future__ import annotations


class SinkError(Exception):
    """Sink 写入/打开/冲刷失败。"""


__all__ = ["SinkError"]


def error_text(error: BaseException | str) -> str:
    """把异常/描述压成一行可读错误文本；空消息异常（如裸 ``TimeoutError``）
    回退为类型名，保证 ``last_error`` 不为空字符串。"""
    if isinstance(error, str):
        return error
    return str(error) or type(error).__name__
