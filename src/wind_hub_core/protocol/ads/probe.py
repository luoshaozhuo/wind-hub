"""ADS 地址解析与可读性主动探测。

本模块属于 wind-hub-core 的协议验证边界，供 Server/Collector 在短生命周期
validation session 中复用。它不参与周期采集、不维护长期 PLC 会话，也不修改
配置文件。

pyads 仅在实际使用 ADS 探测时延迟导入，因此未安装 ADS extra 的环境仍可导入
wind-hub-core。pyads 没有稳定类型标注，第三方对象在本模块内部以 Any 隔离，
不会进入公开验证模型。

变量名解析通过 Connection.get_symbol 逐点执行，并在单个 ADSProbe 实例内缓存
index_group/index_offset；验证读取始终使用解析后的 index 地址。
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
    """延迟导入 pyads，并把未类型化第三方模块限制在适配边界内。

    Returns:
        pyads 模块对象。由于 pyads 未提供完整类型信息，此处使用 Any。

    Raises:
        ImportError: 运行 ADS 探测但环境未安装 pyads。
    """
    # pyads 当前未提供可供 mypy 使用的完整类型信息；待上游发布稳定类型声明后移除抑制。
    import pyads  # type: ignore[import-untyped]

    return pyads


def _plc_type(data_type: str) -> Any:
    """把 Wind Hub 基础类型映射为 pyads PLCTYPE。

    Args:
        data_type: 点表声明的数据类型。

    Returns:
        pyads 对应的 ctypes PLC 类型；第三方类型边界以 Any 表达。

    Raises:
        ValueError: data_type 没有 ADS 类型映射。
        ImportError: 环境未安装 pyads。
    """
    ads_name = _TYPE_MAP.get(data_type.lower())
    if ads_name is None:
        raise ValueError(f"unsupported ADS data_type: {data_type}")
    return getattr(_pyads(), f"PLCTYPE_{ads_name}")


class ADSProbe:
    """短生命周期 ADS 主动验证会话。

    Args:
        target: 待验证 PLC 的连接快照。

    Notes:
        connect 和 close 均幂等；同一实例不会在连接有效时重复创建远端 ADS
        connection。解析缓存只在实例生命周期内有效，不作为采集运行时缓存。
    """

    def __init__(self, target: DeviceProbeTarget) -> None:
        self._target = target
        # pyads Connection 无稳定类型声明，仅在本适配边界内部持有。
        self._connection: Any = None
        self._connected = False
        self._resolved: dict[str, AddressResolution] = {}

    @property
    def connected(self) -> bool:
        """当前验证会话是否已建立 ADS 连接。"""
        return self._connected

    async def connect(self) -> None:
        """建立 ADS 连接；已连接时直接返回。

        Side Effects:
            创建一个 pyads Connection，并设置连接超时。

        Raises:
            ImportError: 环境未安装 pyads。
            ConnectionError: connection.open 返回后连接仍未处于 open 状态。
            Exception: pyads 建连阶段的其他异常原样传播。

        Notes:
            建连失败时会先关闭临时 connection，避免残留本地 ADS 资源。
        """
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
            # connection 可能已经占用本地 ADS 资源；失败路径必须显式释放后再抛出。
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

    async def resolve_points(
        self,
        points: list[PointProbeSpec],
    ) -> dict[str, AddressResolution]:
        """解析点位 ADS 地址，并在当前会话内缓存结果。

        Args:
            points: 待解析的点探测定义。含 symbol 时从 PLC 查询；仅含
                index_group/index_offset 时直接使用配置地址。

        Returns:
            以 point_id 为键的地址解析结果。

        Raises:
            ConnectionError: 当前 ADSProbe 尚未连接。
            ValueError: 点既没有 symbol，也没有完整 index 地址，或 PLC 返回
                非法 index_group/index_offset。
            Exception: pyads get_symbol 的协议异常原样传播。
        """
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

    async def read_value(
        self,
        point: PointProbeSpec,
        resolution: AddressResolution,
    ) -> object:
        """按指定解析地址读取一个 ADS 原始值。

        Args:
            point: 点定义，用于确定 PLC 数据类型。
            resolution: 当前 probe session 内解析得到的 index 地址。

        Returns:
            PLC 原始值。

        Raises:
            ConnectionError: 当前 probe 尚未连接。
            ValueError: resolution 缺少完整 index 地址。
            Exception: pyads read 异常原样传播。
        """
        if not self._connected:
            raise ConnectionError("ADS probe is not connected")
        if resolution.index_group is None or resolution.index_offset is None:
            raise ValueError(
                f"ADS point '{point.point_id}' has no resolved index address"
            )
        return await asyncio.to_thread(
            self._connection.read,
            resolution.index_group,
            resolution.index_offset,
            _plc_type(point.data_type),
        )

    async def verify_read(
        self,
        points: list[PointProbeSpec],
        resolutions: dict[str, AddressResolution],
    ) -> set[str]:
        """按解析后的 index 地址验证点位可读性。

        Args:
            points: 待验证点定义。
            resolutions: 对应 point_id 的地址解析结果。

        Returns:
            成功完成一次 ADS read 的 point_id 集合。

        Raises:
            ConnectionError: 当前 ADSProbe 尚未连接。

        Notes:
            单点读取失败不会终止整批验证；失败点不进入返回集合，由上层生成
            POINT_READ_FAILED 等分层验证结果。
        """
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
                # 主动验证需要收集全部点的结果；单点协议失败由上层统一分类。
                continue
            readable.add(point.point_id)
        return readable
