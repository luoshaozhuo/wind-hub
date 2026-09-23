"""modbus_debug — 现场 Modbus 单机调试工具。

固定读取 ``configs/site_wtg_modbus`` 正式配置（设备、点表、word_order 等），
但不走 wind-hub Runtime / Scheduler / Task / Device / ModbusDriver——实际
通信直接使用 pymodbus ``AsyncModbusTcpClient``，用于独立验证现场 PLC 与
site_wtg_modbus 配置是否匹配。

用法：
    python -m modbus_debug read-all
    python -m modbus_debug watch --device wtg-002
    python -m modbus_debug raw --device wtg-002 --point active_power

word_order 语义（与正式 ModbusDriver 完全一致）：
    big_endian:    低地址寄存器 = 高 16 位
    little_endian: 低地址寄存器 = 低 16 位
"""

from __future__ import annotations

import asyncio
import struct
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from pymodbus.client import AsyncModbusTcpClient

from wind_hub.config.loader import load_config
from wind_hub.config.schema import Config, DeviceConfig, PointConfig

# 固定配置目录（仓库根下的 configs/site_wtg_modbus）
SITE_DIR = Path(__file__).resolve().parents[2] / "configs" / "site_wtg_modbus"

app = typer.Typer(name="modbus_debug", help="现场 Modbus 单机调试工具（读取 site_wtg_modbus 配置）")

# 多寄存器数据类型的 struct 格式（大端字节序；字序在寄存器层面处理）。
_MULTI_REGISTER_FMT: dict[str, str] = {
    "int32": ">i",
    "uint32": ">I",
    "float32": ">f",
    "int64": ">q",
    "uint64": ">Q",
    "float64": ">d",
}

_REGISTER_COUNTS: dict[str, int] = {
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "int64": 4,
    "uint64": 4,
    "float64": 4,
}


# ---------------------------------------------------------------------------
# 配置查找
# ---------------------------------------------------------------------------


def load_site_config(config_dir: Path = SITE_DIR) -> Config:
    """加载 site_wtg_modbus 正式配置。"""
    return load_config(config_dir)


def find_modbus_devices(config: Config) -> list[DeviceConfig]:
    """所有 ``protocol == modbus`` 且 ``enabled`` 的设备。"""
    return [d for d in config.devices.devices if d.protocol == "modbus" and d.enabled]


def find_device(config: Config, device_id: str) -> DeviceConfig:
    """按 ID 查找设备，校验 protocol/enabled。"""
    dev = next((d for d in config.devices.devices if d.device_id == device_id), None)
    if dev is None:
        raise typer.BadParameter(f"设备 '{device_id}' 不存在")
    if dev.protocol != "modbus":
        raise typer.BadParameter(f"设备 '{device_id}' 不是 modbus 协议（{dev.protocol}）")
    if not dev.enabled:
        raise typer.BadParameter(f"设备 '{device_id}' 未启用（enabled=false）")
    return dev


def points_in_group(config: Config, device: DeviceConfig, group: str = "all") -> list[PointConfig]:
    """设备绑定点表中属于指定 point_group 的点。"""
    return [p for p in config.points_for_device(device.device_id) if group in p.point_groups]


def find_point(config: Config, device: DeviceConfig, point_id: str) -> PointConfig:
    """在设备绑定点表中查找指定点。"""
    point = next((p for p in config.points_for_device(device.device_id) if p.point_id == point_id), None)
    if point is None:
        raise typer.BadParameter(f"设备 '{device.device_id}' 点表中不存在点 '{point_id}'")
    return point


# ---------------------------------------------------------------------------
# 点参数解析（复用配置模型字段，不走生产 mapping/driver）
# ---------------------------------------------------------------------------


def point_register_type(point: PointConfig) -> str:
    """点的寄存器类型（input / holding）。"""
    raw = (point.address.model_extra or {}).get("register_type") or point.address.type
    rt = str(raw).lower()
    if rt not in ("input", "holding", "input_register", "holding_register"):
        raise ValueError(f"点 '{point.point_id}': 不支持的寄存器类型 '{raw}'（仅支持 input/holding）")
    return "input" if rt.startswith("input") else "holding"


def point_address(point: PointConfig) -> int:
    raw = (point.address.model_extra or {}).get("address")
    if raw is None:
        raise ValueError(f"点 '{point.point_id}': 缺少 address")
    return int(raw)


def point_count(point: PointConfig) -> int:
    extra = point.address.model_extra or {}
    if extra.get("count") is not None:
        return int(extra["count"])
    return _REGISTER_COUNTS[point.data_type]


def point_word_order(device: DeviceConfig, point: PointConfig) -> str:
    """word_order 覆盖链：point > device > little_endian。"""
    extra = point.address.model_extra or {}
    wo = extra.get("word_order") or device.endpoint.extensions.get("word_order") or "little_endian"
    return str(wo)


# ---------------------------------------------------------------------------
# 解码（独立于生产 ModbusDriver 的私有 decode，语义保持一致）
# ---------------------------------------------------------------------------


def decode_value(registers: list[int], data_type: str, word_order: str) -> Any:
    """按 data_type + word_order 解码寄存器为原始值（未乘 scale）。

    big_endian: 低地址寄存器 = 高 16 位；little_endian: 低地址 = 低 16 位。
    """
    if data_type == "int16":
        w = registers[0]
        return w if w < 0x8000 else w - 0x10000
    if data_type == "uint16":
        return registers[0]

    fmt = _MULTI_REGISTER_FMT[data_type]
    words = list(registers)
    if word_order == "little_endian":
        words = list(reversed(words))
    raw = b"".join(struct.pack(">H", w) for w in words)
    return struct.unpack(fmt, raw)[0]


def word_order_candidates(registers: list[int]) -> dict[str, int]:
    """2 寄存器数据的四种候选解码（int32/uint32 × big/little endian）。"""
    w0, w1 = registers[0], registers[1]
    return {
        "int32 big_endian": struct.unpack(">i", struct.pack(">HH", w0, w1))[0],
        "int32 little_endian": struct.unpack(">i", struct.pack(">HH", w1, w0))[0],
        "uint32 big_endian": struct.unpack(">I", struct.pack(">HH", w0, w1))[0],
        "uint32 little_endian": struct.unpack(">I", struct.pack(">HH", w1, w0))[0],
    }


# ---------------------------------------------------------------------------
# Modbus 通信（直接 pymodbus，不经 ModbusDriver）
# ---------------------------------------------------------------------------


def device_conn_params(device: DeviceConfig) -> tuple[str, int, int, float]:
    """(host, port, unit_id, timeout)。"""
    ext = device.endpoint.extensions
    return (
        device.endpoint.host,
        int(ext.get("port", device.endpoint.port)),
        int(ext.get("unit_id", 1)),
        float(ext.get("timeout", 3.0)),
    )


async def connect_device(device: DeviceConfig) -> AsyncModbusTcpClient:
    """建立 TCP 连接，失败抛异常。"""
    host, port, _, timeout = device_conn_params(device)
    client = AsyncModbusTcpClient(host, port=port, timeout=timeout)
    if not await client.connect():
        client.close()
        raise ConnectionError(f"无法连接 {host}:{port}")
    return client


async def read_registers(
    client: AsyncModbusTcpClient,
    device: DeviceConfig,
    register_type: str,
    address: int,
    count: int,
) -> list[int]:
    """按寄存器类型读取原始寄存器。"""
    _, _, unit_id, _ = device_conn_params(device)
    if register_type == "input":
        resp = await client.read_input_registers(address, count=count, device_id=unit_id)
    else:
        resp = await client.read_holding_registers(address, count=count, device_id=unit_id)
    if resp.isError():
        raise ConnectionError(f"Modbus 异常响应（{register_type} @{address}）: {resp}")
    return list(resp.registers)


async def read_point(
    client: AsyncModbusTcpClient, device: DeviceConfig, point: PointConfig
) -> Any:
    """读取单个点，返回原始解码值（不乘 scale/offset）。"""
    registers = await read_registers(
        client, device, point_register_type(point), point_address(point), point_count(point)
    )
    return decode_value(registers, point.data_type, point_word_order(device, point))


async def read_device_points(
    device: DeviceConfig, points: list[PointConfig]
) -> list[tuple[PointConfig, Any]]:
    """连接一次，逐点读取，关闭连接。失败抛异常。"""
    client = await connect_device(device)
    try:
        results: list[tuple[PointConfig, Any]] = []
        for p in points:
            value = await read_point(client, device, p)
            results.append((p, value))
        return results
    finally:
        client.close()


async def read_all_devices(
    entries: list[tuple[DeviceConfig, list[PointConfig]]],
) -> list[tuple[DeviceConfig, list[tuple[PointConfig, Any]] | Exception]]:
    """并发读取多台设备；单台失败不影响其它设备。"""
    async def _one(
        device: DeviceConfig, points: list[PointConfig]
    ) -> tuple[DeviceConfig, list[tuple[PointConfig, Any]] | Exception]:
        try:
            return device, await read_device_points(device, points)
        except Exception as exc:  # 单台失败隔离
            return device, exc

    return await asyncio.gather(*(_one(d, pts) for d, pts in entries))


# ---------------------------------------------------------------------------
# watch 循环（核心可测试）
# ---------------------------------------------------------------------------


def format_watch(
    device: DeviceConfig,
    results: list[tuple[PointConfig, Any]] | None = None,
    error: Exception | None = None,
) -> str:
    """watch 一帧的纯文本（不含 ANSI 控制码）。

    ``error`` 非空时生成错误帧（读取失败但继续重试）。
    """
    host, _, _, _ = device_conn_params(device)
    lines = [f"device: {device.device_id}", f"host:   {host}", ""]
    if error is not None:
        lines.append(f"读取失败：{error}")
        lines.append("正在继续重试...")
    else:
        lines.append(f"{'point':<22} {'value':>12}")
        lines.append("-" * 36)
        for p, v in results or []:
            lines.append(f"{p.point_id:<22} {v!s:>12}")
    lines.append("")
    lines.append("Ctrl+C 退出")
    return "\n".join(lines)


def render_watch(text: str) -> None:
    """终端原地刷新：首次清屏，之后每帧光标回首页并清除残留。

    首次清屏状态记录在函数属性上（单进程内只有一个 watch 实例）。
    """
    if not getattr(render_watch, "_cleared", False):
        sys.stdout.write("\033[2J")  # 首次：清屏
        render_watch._cleared = True  # type: ignore[attr-defined]
    sys.stdout.write("\033[H")  # 光标回左上角（不整屏清，避免闪烁）
    sys.stdout.write(text)
    sys.stdout.write("\033[J")  # 清除 frame 之后残留的旧内容
    sys.stdout.flush()


async def watch_loop(
    device: DeviceConfig,
    points: list[PointConfig],
    interval: float = 1.0,
    render: Callable[[str], None] | None = None,
) -> None:
    """持续读取并刷新输出，直到被取消（Ctrl+C）；退出前关闭连接。

    单次读取失败渲染完整错误帧并继续；连接在循环内复用，不每次重建。
    ``render`` 为 None 时使用终端原地刷新 :func:`render_watch`。
    """
    if render is None:
        render = render_watch
    client = await connect_device(device)
    try:
        while True:
            try:
                results = [(p, await read_point(client, device, p)) for p in points]
                render(format_watch(device, results))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                render(format_watch(device, error=exc))
            await asyncio.sleep(interval)
    finally:
        client.close()
        if render is render_watch:
            # 补一个换行，避免 shell 提示符紧贴最后一帧
            sys.stdout.write("\n")
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# CLI 命令
# ---------------------------------------------------------------------------


@app.command(name="read-all")
def read_all_cmd(
    config_dir: Path = typer.Option(SITE_DIR, "--config", help="配置目录（默认 configs/site_wtg_modbus）"),
) -> None:
    """读取所有 enabled 的 modbus 设备的 all 组点（各机并发，读完退出）。"""
    config = load_site_config(config_dir)
    entries = [(d, points_in_group(config, d, "all")) for d in find_modbus_devices(config)]
    if not entries:
        typer.echo("没有 enabled 的 modbus 设备", err=True)
        raise typer.Exit(1)

    results = asyncio.run(read_all_devices(entries))
    for device, result in results:
        host, _, _, _ = device_conn_params(device)
        if isinstance(result, Exception):
            typer.echo(f"{device.device_id}  {host}  ERROR  {result}\n")
            continue
        typer.echo(f"{device.device_id}  {host}  OK")
        for p, v in result:
            typer.echo(f"  {p.point_id:<22} {v!s:>12}")
        typer.echo()


@app.command()
def watch(
    device_id: str = typer.Option(..., "--device", help="设备 ID"),
    config_dir: Path = typer.Option(SITE_DIR, "--config", help="配置目录"),
    interval: float = typer.Option(1.0, "--interval", help="刷新间隔（秒）"),
) -> None:
    """持续监视单台设备的 all 组点，Ctrl+C 退出。"""
    config = load_site_config(config_dir)
    device = find_device(config, device_id)
    points = points_in_group(config, device, "all")
    if not points:
        typer.echo(f"设备 '{device_id}' 没有 all 组点", err=True)
        raise typer.Exit(1)
    try:
        asyncio.run(watch_loop(device, points, interval))
    except KeyboardInterrupt:
        pass


@app.command()
def raw(
    device_id: str = typer.Option(..., "--device", help="设备 ID"),
    point_id: str = typer.Option(..., "--point", help="点 ID"),
    config_dir: Path = typer.Option(SITE_DIR, "--config", help="配置目录"),
) -> None:
    """读取指定点的原始寄存器，输出候选字序解码与配置解析结果。"""
    config = load_site_config(config_dir)
    device = find_device(config, device_id)
    point = find_point(config, device, point_id)

    register_type = point_register_type(point)
    address = point_address(point)
    count = point_count(point)
    word_order = point_word_order(device, point)

    async def _read() -> list[int]:
        client = await connect_device(device)
        try:
            return await read_registers(client, device, register_type, address, count)
        finally:
            client.close()

    try:
        registers = asyncio.run(_read())
    except (ConnectionError, OSError) as exc:
        typer.echo(f"读取失败：{exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"{device.device_id}  type={register_type}  address={address}  count={count}")
    for i, w in enumerate(registers):
        typer.echo(f"  address={address + i}  decimal={w}  hex={w:#06x}")

    if count == 2:
        for label, decoded in word_order_candidates(registers).items():
            typer.echo(f"  候选 {label}: {decoded}")

    raw_value = decode_value(registers, point.data_type, word_order)
    typer.echo(f"配置 word_order: {word_order}")
    typer.echo(f"配置解析结果: {raw_value}")
