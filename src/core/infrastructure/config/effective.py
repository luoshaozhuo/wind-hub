"""运行时有效连接参数合成：设备显式 > 型号默认 > 协议代码默认。

ConfigSnapshot 只保存显式配置（Device.endpoint 与
Device.protocol_options 为设备实例显式值，型号默认值留于
DeviceModel）；Collector/Commander 装配层经本模块合成运行时消费的
有效 endpoint（端口三级回退后必然具体）与有效协议参数。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from core.domain import (
    ConnectionEndpoint,
    Device,
    DeviceModel,
    ProtocolOptions,
)
from core.infrastructure.protocol.ads.config import parse_ads_config
from core.infrastructure.protocol.iec104.config import parse_iec104_config
from core.infrastructure.protocol.modbus.config import parse_modbus_config

_OPTION_PARSERS: dict[str, Callable[..., Any]] = {
    "ads": parse_ads_config,
    "modbus": parse_modbus_config,
    "iec104": parse_iec104_config,
}


def resolve_effective_device(
    device: Device,
    model: DeviceModel,
    protocol: str,
) -> tuple[Device, ProtocolOptions]:
    """合成单台设备的有效 endpoint 与有效协议参数。

    端口回退：设备 endpoint.port → 型号 endpoint_defaults.port → 协议
    代码默认（Modbus 502、IEC104 2404、ADS 按 twincat_version 801/851）。
    协议参数回退：设备 protocol_options → 型号 protocol_options → 协议
    代码默认（由 Driver 解析层在 connect 时再次应用，不在此展开）。
    """
    options: ProtocolOptions = {**model.protocol_options, **device.protocol_options}
    endpoint = device.endpoint
    port = endpoint.port or model.endpoint_defaults.get("port")
    if port is None:
        parsed = _OPTION_PARSERS[protocol](ConnectionEndpoint(host=endpoint.host), options)
        # ADS 的有效端口字段名为 target_port，其余协议为 port。
        port = parsed.target_port if protocol == "ads" else parsed.port
    if endpoint.port != port:
        endpoint = ConnectionEndpoint(host=endpoint.host, port=int(port))
        device = replace(device, endpoint=endpoint)
    if dict(device.protocol_options) != dict(options):
        device = replace(device, protocol_options=options)
    return device, options


__all__ = ["resolve_effective_device"]
