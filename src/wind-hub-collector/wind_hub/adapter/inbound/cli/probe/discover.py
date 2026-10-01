"""协议点表发现——ADS 符号浏览与 Modbus 可选寄存器扫描。

ADS 是三协议中唯一能真正「发现」点表的协议（决策 1）：TwinCAT 通过
符号上传接口暴露全部变量的名称/类型/大小/注释。Modbus 没有符号概念，
只能盲扫寄存器地址（风险操作，需 CLI 显式 ``--unsafe`` 才进入本模块的
:func:`discover_modbus`）。IEC104 在 CLI 层直接报「不支持」，不到这里。

pyads 与 pymodbus 的调用方式与对应驱动一致：pyads 同步 API 经
``asyncio.to_thread`` 包裹；pymodbus 用其异步客户端。两个第三方库都按
驱动的惯例**惰性导入**（可选 extra，未安装时给出明确错误而非 import 崩
溃），且不向本模块的公开签名泄漏第三方类型。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from wind_hub.adapter.inbound.cli.probe.models import DiscoveredPoint
from wind_hub.adapter.outbound.protocol.ads.config import (
    ADSConfig,
)
from wind_hub.adapter.outbound.protocol.ads.config import (
    from_device_config as ads_config_from_device,
)
from wind_hub.adapter.outbound.protocol.modbus.config import (
    ModbusConfig,
)
from wind_hub.adapter.outbound.protocol.modbus.config import (
    from_device_config as modbus_config_from_device,
)
from wind_hub.config.schema import DeviceConfig
from wind_hub.domain.model.errors import ProtocolError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ADS 符号类型 → data_type 映射（决策 2）
# ---------------------------------------------------------------------------

# 键为 TwinCAT 类型名（pyads 符号的 data_type 类名去掉 ``PLCTYPE_`` 前缀）。
# 未收录的类型统一归为 ``"unknown"``，草稿里需人工确认后再改。
_ADS_TYPE_MAP: dict[str, str] = {
    "BOOL": "bool",
    "SINT": "int8",
    "INT": "int16",
    "DINT": "int32",
    "LINT": "int64",
    "USINT": "uint8",
    "UINT": "uint16",
    "UDINT": "uint32",
    "ULINT": "uint64",
    "BYTE": "uint8",
    "WORD": "uint16",
    "DWORD": "uint32",
    "REAL": "float32",
    "LREAL": "float64",
    "STRING": "string",
}

# 系统符号前缀（TwinCAT 内部变量，如 ``__INFO``、``__SYMBOLSIZE``）——
# 默认过滤（决策 6）。
_SYSTEM_SYMBOL_PREFIX = "__"


def map_ads_data_type(ads_type_name: str) -> str:
    """TwinCAT 类型名 → points.yaml 的 data_type；未知类型返回 ``"unknown"``。"""
    return _ADS_TYPE_MAP.get(ads_type_name.upper(), "unknown")


def _symbol_type_name(symbol: Any) -> str:
    """从 pyads 符号对象取 TwinCAT 类型名（去掉 ``PLCTYPE_`` 前缀）。

    pyads ``Symbol.data_type`` 是一个 ``PLCTYPE_*`` 类（如
    ``PLCTYPE_REAL``）；拿不到类名时退化为字符串形式再剥离前缀。
    """
    raw = getattr(symbol.data_type, "__name__", None) or str(symbol.data_type)
    return raw.removeprefix("PLCTYPE_")


def _upload_symbols(config: ADSConfig, host: str) -> list[Any]:
    """连接 PLC 并上传全部符号（同步阻塞——调用方负责放到线程里）。

    单独成模块级函数以便测试替换（mock pyads 网络层）。
    """
    import pyads  # type: ignore[import-untyped]

    # 与驱动的 ``_do_connect`` 同款连接参数：target_net_id 为空时交给
    # pyads 按 IP 自动推导。
    net_id = config.target_net_id or None
    connection = pyads.Connection(net_id, config.target_port, host)
    connection.set_timeout(int(config.timeout * 1000))
    try:
        connection.open()
        return list(connection.get_all_symbols())
    finally:
        connection.close()


async def discover_ads(
    device_cfg: DeviceConfig,
    filter_prefix: str | None = None,
) -> list[DiscoveredPoint]:
    """ADS 符号浏览——上传 PLC 符号表并转换为发现点列表。

    流程：连接 → 符号上传 → 过滤系统符号与前缀 → 类型映射。
    符号数可能上万；pyads 的 ``get_all_symbols`` 一次性返回完整列表
    （其内部已按 ADS 帧分批传输），数量告警由 CLI 层负责（决策 7）。

    Args:
        device_cfg: 设备配置（``endpoint`` 提供 host 与 ADS 扩展参数）。
        filter_prefix: 只保留以该前缀开头的符号名；``None`` 不过滤。

    Raises:
        ProtocolError: 连接或符号上传失败。
    """
    config = ads_config_from_device(device_cfg)
    host = device_cfg.endpoint.host
    try:
        symbols = await asyncio.to_thread(_upload_symbols, config, host)
    except ProtocolError:
        raise
    except Exception as exc:
        raise ProtocolError(
            f"ADS symbol upload failed for {host}:{config.target_port}: {exc}"
        ) from exc

    points: list[DiscoveredPoint] = []
    for symbol in symbols:
        name: str = symbol.name
        if name.startswith(_SYSTEM_SYMBOL_PREFIX):
            continue
        if filter_prefix is not None and not name.startswith(filter_prefix):
            continue
        points.append(
            DiscoveredPoint(
                symbol=name,
                data_type=map_ads_data_type(_symbol_type_name(symbol)),
                size=int(symbol.size),
                comment=symbol.comment or None,
            )
        )
    return points


# ---------------------------------------------------------------------------
# Modbus 寄存器扫描（决策 8，仅 --unsafe）
# ---------------------------------------------------------------------------


def parse_scan_ranges(spec: str) -> list[tuple[int, int]]:
    """解析 ``--range`` 参数为 ``[(start, end), ...]``（闭区间）。

    支持逗号分隔的多段与单地址：``"0-1000"``、``"0-100,200-300"``、
    ``"500"``。地址范围 0..65535（Modbus 协议地址空间）。

    Raises:
        ValueError: 格式非法或越界。
    """
    ranges: list[tuple[int, int]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty range segment in {spec!r}")
        if "-" in part:
            start_s, _, end_s = part.partition("-")
        else:
            start_s = end_s = part
        try:
            start, end = int(start_s), int(end_s)
        except ValueError:
            raise ValueError(f"invalid range segment {part!r}") from None
        if not (0 <= start <= end <= 65535):
            raise ValueError(f"range {part!r} out of bounds; expected 0 <= start <= end <= 65535")
        ranges.append((start, end))
    return ranges


def _create_modbus_client(config: ModbusConfig) -> Any:
    """创建 pymodbus 异步 TCP 客户端（惰性导入；单独成函数以便测试替换）。"""
    from pymodbus.client import AsyncModbusTcpClient

    return AsyncModbusTcpClient(config.host, port=config.port, timeout=config.timeout)


async def discover_modbus(
    device_cfg: DeviceConfig,
    ranges: list[tuple[int, int]],
) -> list[DiscoveredPoint]:
    """Modbus 保持寄存器扫描——逐地址试读，记录可读的地址。

    **风险操作**（决策 8）：盲扫可能触发从站的异常保护、返回无意义数
    据，且大范围扫描很慢。CLI 层只在用户显式传 ``--unsafe`` 时才调用
    本函数。每个地址读 1 个保持寄存器；异常响应（illegal address 等）
    与传输错误都视为「该地址不可读」而跳过。

    返回的 :class:`DiscoveredPoint` 为合成条目：``symbol`` 形如
    ``holding[100]``，``address`` 携带寄存器寻址，``comment`` 固定标注
    「扫描结果，需人工确认」。

    Raises:
        ProtocolError: 无法建立 TCP 连接。
    """
    config = modbus_config_from_device(device_cfg)
    client = _create_modbus_client(config)
    try:
        connected = await client.connect()
        if not connected:
            raise ProtocolError(
                f"Modbus: cannot connect to {config.host}:{config.port} for scanning"
            )
        points: list[DiscoveredPoint] = []
        for start, end in ranges:
            for address in range(start, end + 1):
                try:
                    response = await client.read_holding_registers(
                        address, count=1, device_id=config.unit_id
                    )
                except Exception:  # 传输错误——该地址按不可读处理，继续扫描
                    continue
                if response.isError():
                    continue
                points.append(
                    DiscoveredPoint(
                        symbol=f"holding[{address}]",
                        data_type="int16",
                        size=2,
                        comment="扫描结果，需人工确认",
                        address={"register_type": "holding", "address": address},
                    )
                )
        return points
    finally:
        client.close()
