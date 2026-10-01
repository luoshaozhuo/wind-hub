"""ADS 地址解析与可读性探测。

按变量名逐个调用 pyads get_symbol 获取 index_group/index_offset。pyads 当前
公开 API 没有“多个 name 一次返回全部地址”的 Connection 方法，因此一次
validation session 内只解析一次并缓存，不把 symbol lookup 放进周期采集。
"""

from __future__ import annotations

import asyncio
import ctypes
from typing import Any

from wind_hub_core.validation.models import (
    AddressResolution,
    DeviceProbeTarget,
    PointProbeSpec,
)

_TYPE_MAP = {
    "bool": "BOOL",
    "int8": "SINT",
    "uint8": "USINT",
    "int16": "INT",
    "uint16": "UINT",
    "int32": "DINT",
    "uint32": "UDINT",
    "float32": "REAL",
    "float64": "LREAL",
    "str": "STRING",
}


def _pyads() -> Any:
    """延迟导入 pyads，保持无 ADS 依赖环境仍可导入 core。"""
    import pyads  # type: ignore[import-untyped]

    return pyads


def _plc_type(data_type: str) -> Any:
    """将 Wind Hub 基础类型映射为 pyads PLCTYPE。"""
    ads_name = _TYPE_MAP.get(data_type.lower())
    if ads_name is None:
        raise ValueError(f"unsupported ADS data_type: {data_type}")
    return getattr(_pyads(), f"PLCTYPE_{ads_name}")


class ADSProbe:
    """短生命周期 ADS 验证 session。

    connect/close 幂等；同一实例不会在连接仍有效时重复连接远端 PLC。
    """

    def __init__(self, target: DeviceProbeTarget) -> None:
        self._target = target
        self._connection: Any = None
        self._connected = False
        self._resolved: dict[str, AddressResolution] = {}

    @property
    def connected(self) -> bool:
        """当前 session 是否已连接。"""
        return self._connected

    async def connect(self) -> None:
        """建立 ADS 连接；已连接时直接返回。"""
        if self._connected:
            return
        pyads = _pyads()
        options = self._target.options
        net_id = str(options.get("target_net_id") or "") or None
        twincat_version = str(options.get("twincat_version", "2"))
        default_port = 851 if twincat_version == "3" else 801
        target_port = int(
            options.get("target_port", options.get("ams_port", default_port))
        )
        timeout = float(options.get("timeout", 5.0))
        connection = pyads.Connection(net_id, target_port, self._target.host)
        connection.set_timeout(int(timeout * 1000))
        try:
            await asyncio.to_thread(connection.open)
            if not connection.is_open:
                raise ConnectionError(
                    f"ADS connection is not open: {self._target.device_id}"
                )
        except Exception:
            await asyncio.to_thread(connection.close)
            raise
        self._connection = connection
        self._connected = True

    async def close(self) -> None:
        """关闭 ADS session。"""
        connection, self._connection = self._connection, None
        self._connected = False
        if connection is not None:
            await asyncio.to_thread(connection.close)

    async def resolve_points(
        self,
        points: list[PointProbeSpec],
    ) -> dict[str, AddressResolution]:
        """逐 symbol 解析 index 地址，并在本 session 内缓存。"""
        if not self._connected:
            raise ConnectionError("ADS probe is not connected")

        result: dict[str, AddressResolution] = {}
        for point in points:
            cached = self._resolved.get(point.point_id)
            if cached is not None:
                result[point.point_id] = cached
                continue

            address = point.address
            symbol_value = address.get("symbol")
            symbol = str(symbol_value) if symbol_value is not None else None
            configured_group = address.get("index_group")
            configured_offset = address.get("index_offset")

            if symbol is None:
                if configured_group is None or configured_offset is None:
                    raise ValueError(
                        f"ADS point '{point.point_id}' has neither symbol nor index address"
                    )
                resolved = AddressResolution(
                    point_id=point.point_id,
                    symbol=None,
                    index_group=int(configured_group),
                    index_offset=int(configured_offset),
                    protocol_type=point.data_type,
                )
            else:
                remote = await asyncio.to_thread(self._connection.get_symbol, symbol)
                index_group = getattr(remote, "index_group", None)
                index_offset = getattr(remote, "index_offset", None)
                if not isinstance(index_group, int) or not isinstance(index_offset, int):
                    raise ValueError(
                        f"ADS symbol '{symbol}' returned invalid index address"
                    )
                plc_type = getattr(remote, "plc_type", None)
                size: int | None = None
                if plc_type is not None:
                    size = ctypes.sizeof(plc_type)
                resolved = AddressResolution(
                    point_id=point.point_id,
                    symbol=symbol,
                    index_group=index_group,
                    index_offset=index_offset,
                    size=size,
                    protocol_type=str(getattr(remote, "symbol_type", "") or ""),
                )

            self._resolved[point.point_id] = resolved
            result[point.point_id] = resolved
        return result

    async def verify_read(
        self,
        points: list[PointProbeSpec],
        resolutions: dict[str, AddressResolution],
    ) -> set[str]:
        """按解析后的 index 地址逐点验证可读性。"""
        if not self._connected:
            raise ConnectionError("ADS probe is not connected")

        readable: set[str] = set()
        for point in points:
            resolved = resolutions.get(point.point_id)
            if (
                resolved is None
                or resolved.index_group is None
                or resolved.index_offset is None
            ):
                continue
            try:
                await asyncio.to_thread(
                    self._connection.read,
                    resolved.index_group,
                    resolved.index_offset,
                    _plc_type(point.data_type),
                )
            except Exception:
                continue
            readable.add(point.point_id)
        return readable
