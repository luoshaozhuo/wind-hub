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
    区分：只有本类故障允许 RecoveringProtocol 按恢复策略重试。
    """


class ProtocolCapabilityError(ProtocolError):
    """当前协议不支持调用方请求的协议能力。"""
