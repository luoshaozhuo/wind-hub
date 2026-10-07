"""Shared Core 稳定应用异常。"""

from __future__ import annotations


class CoreError(Exception):
    """Shared Core 对上层应用暴露的稳定语义异常基类。"""


class ConfigError(CoreError):
    """共享配置缺失、格式非法、引用不一致或接入定义不可用。"""


class ProtocolError(CoreError):
    """协议建连、握手、读写或链路级失败。"""
