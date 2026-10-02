"""Collector 旧 outbound protocol 包。

IEC104 兼容入口将在后续协议收敛阶段移除；ADS 与 Modbus 已直接使用 wind-hub-core。
"""

from wind_hub.adapter.outbound.protocol.iec104.driver import IEC104Driver

__all__ = ["IEC104Driver"]
