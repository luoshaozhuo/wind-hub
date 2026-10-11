"""Shared Core 稳定应用异常。"""

from __future__ import annotations


class CoreError(Exception):
    """Shared Core 对上层应用暴露的稳定语义异常基类。"""


class ConfigError(CoreError):
    """共享配置缺失、格式非法、引用不一致或接入定义不可用。"""


class ProtocolError(CoreError):
    """协议建连、握手、读写或链路级失败。"""


class ProtocolConnectionError(ProtocolError):
    """通信链路级失败（传输断开、对端无响应、I/O 超时）。

    与设备已正常应答的业务/数据级 ``ProtocolError``（如 Modbus 异常响应）
    区分：本类表示通信链路本身不可用，可由 Watchdog 在后续周期触发重连。
    """


class ProtocolCapabilityError(ProtocolError):
    """当前协议不支持调用方请求的协议能力。"""
