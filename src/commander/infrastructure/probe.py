"""Commander 诊断网络与 ADS 地址探测（Infrastructure）。

``AdsDiagnosticProbe`` 是短生命周期 ADS 地址解析会话，仅服务诊断，不参与
生产读写路径（生产读写由 Core ADSDriver 承担）。

ADS 诊断使用必需依赖 pyads，第三方动态类型保持在基础设施层。
"""

from __future__ import annotations

import asyncio
import ctypes
from typing import Any

import pyads  # type: ignore[import-untyped]

from core.domain import ConnectionEndpoint, Point, ProtocolOptions

from ..application.diagnostic import ResolvedAddress

_TYPE_MAP = {
    "bool": "BOOL",
    "int8": "SINT",
    "uint8": "USINT",
    "int16": "INT",
    "uint16": "UINT",
    "int32": "DINT",
    "uint32": "UDINT",
    "int64": "LINT",
    "uint64": "ULINT",
    "float32": "REAL",
    "float64": "LREAL",
    "str": "STRING",
}


class AdsDiagnosticProbe:
    """短生命周期 ADS symbol 地址解析/验证会话（诊断专用）。

    connect / close 幂等；解析结果仅在实例生命周期内缓存。
    """

    def __init__(
        self,
        endpoint: ConnectionEndpoint,
        options: ProtocolOptions,
    ) -> None:
        self._endpoint = endpoint
        self._options = options
        self._connection: Any = None
        self._connected = False
        self._resolved: dict[str, ResolvedAddress] = {}

    async def connect(self) -> None:
        """建立 ADS 连接；已连接时直接返回。失败时先释放本地 ADS 资源。"""
        if self._connected:
            return
        net_id = str(self._options.get("target_net_id") or "") or None
        timeout = float(self._options.get("timeout", 5.0) or 5.0)
        connection = pyads.Connection(net_id, self._endpoint.port, self._endpoint.host)
        connection.set_timeout(int(timeout * 1000))
        try:
            await asyncio.to_thread(connection.open)
            if not connection.is_open:
                raise ConnectionError(f"ADS connection is not open: {self._endpoint.host}")
        except Exception:
            await asyncio.to_thread(connection.close)
            raise
        self._connection = connection
        self._connected = True

    async def close(self) -> None:
        """关闭 ADS 验证会话并释放 connection；重复调用安全。"""
        connection, self._connection = self._connection, None
        self._connected = False
        if connection is not None:
            await asyncio.to_thread(connection.close)

    async def resolve(self, point: Point) -> ResolvedAddress:
        """解析单点 ADS 地址；含 symbol 时实际查询 PLC 并缓存。"""
        if not self._connected:
            raise ConnectionError("ADS probe is not connected")
        cached = self._resolved.get(point.point_id)
        if cached is not None:
            return cached

        symbol_value = point.ext.get("symbol")
        symbol = str(symbol_value) if symbol_value is not None else None
        configured_group = point.ext.get("index_group")
        configured_offset = point.ext.get("index_offset")
        data_type = str(point.ext.get("data_type", ""))

        if symbol is None:
            if configured_group is None or configured_offset is None:
                raise ValueError(
                    f"ADS point '{point.point_id}' has neither symbol nor " "index address"
                )
            resolved = ResolvedAddress(
                symbol=None,
                index_group=int(str(configured_group)),
                index_offset=int(str(configured_offset)),
                size=None,
                protocol_type=data_type,
            )
        else:
            remote = await asyncio.to_thread(self._connection.get_symbol, symbol)
            index_group = getattr(remote, "index_group", None)
            index_offset = getattr(remote, "index_offset", None)
            if not isinstance(index_group, int) or not isinstance(index_offset, int):
                raise ValueError(f"ADS symbol '{symbol}' returned invalid index address")
            plc_type = getattr(remote, "plc_type", None)
            size = ctypes.sizeof(plc_type) if plc_type is not None else None
            resolved = ResolvedAddress(
                symbol=symbol,
                index_group=index_group,
                index_offset=index_offset,
                size=size,
                protocol_type=str(getattr(remote, "symbol_type", "") or ""),
            )

        self._resolved[point.point_id] = resolved
        return resolved

def _plc_type(data_type: str) -> Any:
    """把基础类型映射为 pyads PLCTYPE。"""
    ads_name = _TYPE_MAP.get(data_type.lower())
    if ads_name is None:
        raise ValueError(f"unsupported ADS data_type: {data_type}")
    return getattr(pyads, f"PLCTYPE_{ads_name}")


__all__ = [
    "AdsDiagnosticProbe",
]
