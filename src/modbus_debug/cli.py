"""modbus_debug — 现场 Modbus 单机调试工具（最简版，只测 wtg-002）。

点表以字面量维护在下方 POINTS（来源：configs/site_wtg_modbus 的
points.yaml）。每次命令只连接 wtg-002（192.168.100.102），读完即断开。

用法：
    python -m modbus_debug read
    python -m modbus_debug read --point active_power

地址为 0-based protocol offset，现场文档值是否需要 -1 待现场确认。
S32 = int32（2 寄存器，高字在前 big_endian）；S16 = int16（1 寄存器）。
读取值 = 原始值 × scale。
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

import typer
from pymodbus.client import AsyncModbusTcpClient

# wtg-002 连接参数
HOST = "192.168.100.102"
PORT = 502
UNIT_ID = 1
TIMEOUT = 3.0


@dataclass(frozen=True)
class Point:
    point_id: str
    address: int
    data_type: str  # "int32" | "int16"
    scale: float
    unit: str = ""


POINTS: list[Point] = [
    Point("active_power", 178, "int32", 0.001, "kW"),
    Point("active_power_1min", 204, "int32", 0.001, "kW"),
    Point("reactive_power", 180, "int32", 0.001, "kVar"),
    Point("wind_speed", 357, "int16", 0.01, "m/s"),
    Point("generator_speed", 104, "int32", 0.01, "rpm"),
    Point("pitch_angle", 859, "int32", 0.01, "deg"),
    Point("fault_code", 19, "int16", 1.0),
    Point("turbine_status", 0, "int16", 1.0),
]


app = typer.Typer(name="modbus_debug", help="现场 Modbus 单机调试工具（最简版）")


def decode(point: Point, registers: list[int]) -> float:
    """按 data_type 解码寄存器并乘 scale（int32 高字在前 big_endian）。"""
    if point.data_type == "int32":
        raw = struct.unpack(">i", struct.pack(">HH", registers[1], registers[0]))[0]
    else:  # int16
        raw = registers[0]
        if raw >= 0x8000:
            raw -= 0x10000
    return raw * point.scale


async def read_all(points: list[Point]) -> list[tuple[Point, float | str]]:
    """连接 wtg-002，逐点读取一次，返回 (点, 值或错误信息) 列表。"""
    results: list[tuple[Point, float | str]] = []
    client = AsyncModbusTcpClient(HOST, port=PORT, timeout=TIMEOUT)
    try:
        if not await client.connect():
            raise ConnectionError(f"无法连接 {HOST}:{PORT}")
        for p in points:
            count = 2 if p.data_type == "int32" else 1
            resp = await client.read_input_registers(p.address, count=count, device_id=UNIT_ID)
            if resp.isError():
                results.append((p, f"ERROR: {resp}"))
            else:
                results.append((p, decode(p, resp.registers)))
    finally:
        client.close()
    return results


@app.command()
def read(
    point_id: str | None = typer.Option(None, "--point", help="单点 ID（缺省读全部点）"),
) -> None:
    """一次读取 wtg-002 的全部点或单个点。"""
    points = POINTS
    if point_id is not None:
        points = [p for p in POINTS if p.point_id == point_id]
        if not points:
            typer.echo(f"错误：未知点 '{point_id}'", err=True)
            raise typer.Exit(1)

    try:
        results = asyncio.run(read_all(points))
    except (ConnectionError, OSError) as exc:
        typer.echo(f"读取失败：{exc}", err=True)
        raise typer.Exit(1) from exc

    for p, value in results:
        typer.echo(f"{p.point_id}  value={value}  unit={p.unit}")


@app.command(name="raw")
def raw(
    point_id: str = typer.Option(..., "--point", help="点 ID（输出该点寄存器原值与 32bit 候选解码）"),
) -> None:
    """读取指定点的原始寄存器，输出十进制/十六进制及 32bit 候选解码。

    int16 点（count=1）只输出原值；int32 点（count=2）额外输出
    big_endian / little_endian 两种字序的有符号/无符号候选。
    """
    point = next((p for p in POINTS if p.point_id == point_id), None)
    if point is None:
        typer.echo(f"错误：未知点 '{point_id}'", err=True)
        raise typer.Exit(1)

    count = 2 if point.data_type == "int32" else 1

    async def _read() -> list[int]:
        client = AsyncModbusTcpClient(HOST, port=PORT, timeout=TIMEOUT)
        try:
            if not await client.connect():
                raise ConnectionError(f"无法连接 {HOST}:{PORT}")
            resp = await client.read_input_registers(
                point.address, count=count, device_id=UNIT_ID
            )
            if resp.isError():
                raise ConnectionError(f"Modbus 异常响应：{resp}")
            return list(resp.registers)
        finally:
            client.close()

    try:
        registers = asyncio.run(_read())
    except (ConnectionError, OSError) as exc:
        typer.echo(f"读取失败：{exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"wtg-002  type=input  address={point.address}  count={count}")
    for i, w in enumerate(registers):
        typer.echo(f"  address={point.address + i}  decimal={w}  hex={w:#06x}")
    if count == 2:
        w0, w1 = registers
        candidates = {
            "int32 big_endian": struct.unpack(">i", struct.pack(">HH", w0, w1))[0],
            "int32 little_endian": struct.unpack(">i", struct.pack(">HH", w1, w0))[0],
            "uint32 big_endian": struct.unpack(">I", struct.pack(">HH", w0, w1))[0],
            "uint32 little_endian": struct.unpack(">I", struct.pack(">HH", w1, w0))[0],
        }
        for label, decoded in candidates.items():
            scaled = decoded * point.scale
            typer.echo(f"  候选 {label}: {decoded}  →  {scaled} {point.unit}")
