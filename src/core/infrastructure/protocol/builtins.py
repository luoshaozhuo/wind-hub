"""Shared Core 内置协议 Registry 的显式装配。"""

from __future__ import annotations

from .ads import ADSDriver
from .iec104 import IEC104Driver
from .modbus import ModbusDriver
from .registry import ProtocolRegistry


def build_protocol_registry() -> ProtocolRegistry:
    """构造注册全部 Shared Core 内置 Driver 的独立 Registry。

    本函数不创建连接、不导入可选协议库的运行时对象，也没有模块级注册副作用。
    Collector 与 Commander 应各自在 Composition Root 中调用一次。
    """
    registry = ProtocolRegistry()
    registry.register("ads", ADSDriver)
    registry.register("iec104", IEC104Driver)
    registry.register("modbus", ModbusDriver)
    return registry
