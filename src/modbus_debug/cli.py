"""modbus_debug CLI — 现场 Modbus 单机调试命令。

命令（每次操作：load config → 找 device → 校验 protocol == modbus →
建立 ModbusDriver / Device → 注入点映射 → connect → 执行一次操作 →
finally close → 退出）：

- ``read``           一次读取全部采集点（point_group=all）或单点（经
  ``Device``，scale/offset 与正式采集一致）；
- ``write``          写 holding/coil 控制点（需 ``--confirm``；写入的是
  协议值/配置定义值，不做 scale/offset 逆变换）；
- ``raw-read``       原始寄存器读取（不经点表/scale），用于现场排查；
- ``address-check``  只读 ``address-1/address/address+1`` 三个地址，快速
  判断现场文档地址是否存在 ±1 偏移（0-based vs 1-based）。

byte_order 语义与 :mod:`wind_hub.adapter.outbound.protocol.modbus.driver`
完全一致：``big_endian`` = 低地址寄存器放高 16 位字；``little_endian`` =
低地址寄存器放低 16 位字。
"""

from __future__ import annotations

import asyncio
import struct
import uuid
from dataclasses import dataclass
from typing import Any

import typer

from wind_hub.adapter.outbound.protocol.modbus.config import ModbusConfig, from_device_config
from wind_hub.adapter.outbound.protocol.modbus.driver import ModbusDriver
from wind_hub.adapter.outbound.protocol.modbus.mapping import ModbusPoint, parse_point
from wind_hub.application.runtime.device import Device
from wind_hub.config.loader import load_config
from wind_hub.config.schema import Config, DeviceConfig, PointConfig
from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.errors import WindHubError
from wind_hub.domain.model.point import PointRef, PointValue

app = typer.Typer(name="modbus_debug", help="现场 Modbus 单机调试工具（独立于 wind-hub Runtime）")

# 可写寄存器类型（与 driver 的只读集合互补）。
_WRITABLE_TYPES = frozenset({"coil", "holding"})

# raw-read / address-check 只支持 16bit 寄存器类型（位类型无地址偏移语义）。
_WORD_TYPES = frozenset({"input", "holding"})


# ---------------------------------------------------------------------------
# 配置 / 设备装配
# ---------------------------------------------------------------------------


def find_device_config(cfg: Config, device_id: str) -> DeviceConfig:
    """按 device_id 找设备配置，并校验 protocol == modbus。

    Raises:
        typer.Exit: 设备不存在或协议不是 modbus。
    """
    dev = next((d for d in cfg.devices.devices if d.device_id == device_id), None)
    if dev is None:
        typer.echo(f"错误：设备 '{device_id}' 不在配置中", err=True)
        raise typer.Exit(1)
    if dev.protocol != "modbus":
        typer.echo(
            f"错误：设备 '{device_id}' 协议为 '{dev.protocol}'，modbus_debug 只支持 modbus",
            err=True,
        )
        raise typer.Exit(1)
    return dev


def build_device(cfg: Config, dev_cfg: DeviceConfig) -> Device:
    """由设备配置建立 ``ModbusDriver`` + ``Device`` 并注入点映射。"""
    driver = ModbusDriver(dev_cfg)
    points = cfg.points_for_device(dev_cfg.device_id)
    driver.set_points_mapping(points)
    return Device(dev_cfg, points, driver)


def find_point(cfg: Config, dev_cfg: DeviceConfig, point_id: str) -> PointConfig:
    """在设备绑定点表中找点。

    Raises:
        typer.Exit: 点不存在。
    """
    point = next(
        (p for p in cfg.points_for_device(dev_cfg.device_id) if p.point_id == point_id), None
    )
    if point is None:
        typer.echo(f"错误：设备 '{dev_cfg.device_id}' 点表中不存在点 '{point_id}'", err=True)
        raise typer.Exit(1)
    return point


def _parse_modbus_point(point: PointConfig) -> ModbusPoint:
    """解析点为 ModbusPoint（复用生产 mapping 代码）。"""
    try:
        return parse_point(point)
    except WindHubError as exc:
        typer.echo(f"错误：点 '{point.point_id}' 地址解析失败：{exc}", err=True)
        raise typer.Exit(1) from exc


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


async def read_device(
    cfg: Config, dev_cfg: DeviceConfig, point_id: str | None
) -> tuple[list[PointValue], list[PointConfig]]:
    """connect → 读一次（all 组全部点或单点）→ finally close。

    读取经 ``Device``，scale/offset 换算与正式采集一致。返回
    ``(读到的值, 设备点表)``——点表用于调用方展示 variable_name/unit。
    """
    device = build_device(cfg, dev_cfg)
    await device.connect()
    try:
        if point_id is None:
            values = await device.read("all")
        else:
            find_point(cfg, dev_cfg, point_id)  # 点必须存在，否则 read 返回 BAD
            values = await device.read_points(
                [PointRef(device_id=dev_cfg.device_id, point_id=point_id)]
            )
        return values, device.points
    finally:
        await device.close()


@app.command()
def read(
    config: str = typer.Option(..., "--config", help="配置目录（如 configs/site_wtg_modbus）"),
    device_id: str = typer.Option(..., "--device", help="设备 ID"),
    point_id: str | None = typer.Option(None, "--point", help="单点 ID（缺省读取 all 组全部点）"),
) -> None:
    """一次读取全部采集点（all 组）或单个点。"""
    try:
        cfg = load_config(config)
        dev_cfg = find_device_config(cfg, device_id)
        values, points = asyncio.run(read_device(cfg, dev_cfg, point_id))
    except WindHubError as exc:
        typer.echo(f"读取失败：{exc}", err=True)
        raise typer.Exit(1) from exc

    meta = {p.point_id: p for p in points}
    for v in values:
        p = meta.get(v.point_id)
        typer.echo(
            f"{v.device_id}  {v.point_id}"
            f"  variable={p.variable_name if p else ''}"
            f"  value={v.value}"
            f"  quality={v.quality.value}"
            f"  unit={p.unit if p and p.unit else ''}"
        )


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def _coerce_value(raw: str) -> Any:
    """把 CLI 字符串按 int/float/bool/str 依次解析（与 ``wind-hub cmd send`` 一致）。"""
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


async def write_device(
    cfg: Config, dev_cfg: DeviceConfig, point_id: str, value: Any
) -> CommandResult:
    """connect → 写一次 → finally close。

    写入的是协议值/配置定义值——``Device.write`` 不做 scale/offset 逆变换，
    本工具也不做。
    """
    device = build_device(cfg, dev_cfg)
    await device.connect()
    try:
        cmd = Command(
            command_id=str(uuid.uuid4()),
            device_id=dev_cfg.device_id,
            point_id=point_id,
            value=value,
        )
        return (await device.write([cmd]))[0]
    finally:
        await device.close()


@app.command()
def write(
    config: str = typer.Option(..., "--config", help="配置目录（如 configs/site_wtg_modbus）"),
    device_id: str = typer.Option(..., "--device", help="设备 ID"),
    point_id: str = typer.Option(..., "--point", help="要写入的点 ID（holding/coil）"),
    value: str = typer.Option(..., "--value", help="写入的协议值/配置定义值（不做换算）"),
    confirm: bool = typer.Option(False, "--confirm", help="确认执行真实写入（缺省拒绝）"),
) -> None:
    """写单个控制点（holding/coil）；input/discrete_input 明确拒绝。"""
    if not confirm:
        typer.echo("拒绝写入：未提供 --confirm（现场写操作必须显式确认）", err=True)
        raise typer.Exit(1)

    try:
        cfg = load_config(config)
        dev_cfg = find_device_config(cfg, device_id)
        point = find_point(cfg, dev_cfg, point_id)
    except WindHubError as exc:
        typer.echo(f"写入失败：{exc}", err=True)
        raise typer.Exit(1) from exc

    mp = _parse_modbus_point(point)
    if mp.register_type not in _WRITABLE_TYPES:
        typer.echo(
            f"拒绝写入：点 '{point_id}' 是只读类型 '{mp.register_type}'" "（只允许 holding/coil）",
            err=True,
        )
        raise typer.Exit(1)

    coerced = _coerce_value(value)
    typer.echo(
        f"写入协议值（未做 scale/offset 逆变换）：device={dev_cfg.device_id}"
        f" point={point_id} register_type={mp.register_type} address={mp.address}"
        f" data_type={point.data_type} value={coerced!r}"
    )

    try:
        result = asyncio.run(write_device(cfg, dev_cfg, point_id, coerced))
    except WindHubError as exc:
        typer.echo(f"写入失败：{exc}", err=True)
        raise typer.Exit(1) from exc

    if result.success:
        typer.echo("写入成功")
    else:
        typer.echo(f"写入失败：{result.error}", err=True)
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# raw-read / address-check —— 原始寄存器诊断（不经点表 / scale）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawRegister:
    """一个 16bit 寄存器的原始读取结果。"""

    address: int
    value: int


async def raw_read_registers(
    dev_cfg: DeviceConfig, register_type: str, address: int, count: int
) -> list[RawRegister]:
    """直接用 pymodbus 连接参数做一次原始 Modbus 请求（不经点表/scale）。

    Raises:
        WindHubError: 连接失败或对端返回异常响应。
    """
    from pymodbus.client import AsyncModbusTcpClient

    mb: ModbusConfig = from_device_config(dev_cfg)
    client = AsyncModbusTcpClient(mb.host, port=mb.port, timeout=mb.timeout)
    try:
        if not await client.connect():
            raise WindHubError(f"无法连接 {mb.host}:{mb.port}")
        if register_type == "input":
            resp = await client.read_input_registers(address, count=count, device_id=mb.unit_id)
        else:  # holding
            resp = await client.read_holding_registers(address, count=count, device_id=mb.unit_id)
        if resp.isError():
            raise WindHubError(f"原始读取返回 Modbus 异常响应（address={address}）")
        return [RawRegister(address=address + i, value=w) for i, w in enumerate(resp.registers)]
    except WindHubError:
        raise
    except Exception as exc:
        # pymodbus 传输层异常不向上泄漏——统一包装为 WindHubError。
        raise WindHubError(f"raw-read 传输失败：{exc}") from exc
    finally:
        client.close()


def _word_order_candidates(w0: int, w1: int) -> dict[str, int]:
    """两个寄存器的 32bit 候选解码（语义与 driver 的 byte_order 一致）。"""
    be = struct.pack(">HH", w0, w1)  # big_endian：低地址放高字
    le = struct.pack(">HH", w1, w0)  # little_endian：低地址放低字
    return {
        "int32 big_endian": struct.unpack(">i", be)[0],
        "int32 little_endian": struct.unpack(">i", le)[0],
        "uint32 big_endian": struct.unpack(">I", be)[0],
        "uint32 little_endian": struct.unpack(">I", le)[0],
    }


def _validate_word_type(register_type: str) -> None:
    if register_type not in _WORD_TYPES:
        typer.echo(f"错误：--type 只支持 {sorted(_WORD_TYPES)}（位类型无寄存器偏移语义）", err=True)
        raise typer.Exit(1)


@app.command(name="raw-read")
def raw_read(
    config: str = typer.Option(..., "--config", help="配置目录（如 configs/site_wtg_modbus）"),
    device_id: str = typer.Option(..., "--device", help="设备 ID"),
    register_type: str = typer.Option(..., "--type", help="寄存器类型：input / holding"),
    address: int = typer.Option(..., "--address", help="起始地址（0-based protocol offset）"),
    count: int = typer.Option(..., "--count", help="读取寄存器个数"),
) -> None:
    """原始寄存器读取（不经点表/scale）；count==2 时输出 32bit 候选解码。"""
    _validate_word_type(register_type)
    if count <= 0:
        typer.echo("错误：--count 必须 > 0", err=True)
        raise typer.Exit(1)

    try:
        cfg = load_config(config)
        dev_cfg = find_device_config(cfg, device_id)
        registers = asyncio.run(raw_read_registers(dev_cfg, register_type, address, count))
    except WindHubError as exc:
        typer.echo(f"raw-read 失败：{exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"{dev_cfg.device_id}  type={register_type}  address={address}  count={count}")
    for reg in registers:
        typer.echo(f"  address={reg.address}  decimal={reg.value}  hex={reg.value:#06x}")
    if count == 2 and len(registers) == 2:
        for label, decoded in _word_order_candidates(
            registers[0].value, registers[1].value
        ).items():
            typer.echo(f"  候选 {label}: {decoded}")


@app.command(name="address-check")
def address_check(
    config: str = typer.Option(..., "--config", help="配置目录（如 configs/site_wtg_modbus）"),
    device_id: str = typer.Option(..., "--device", help="设备 ID"),
    register_type: str = typer.Option(..., "--type", help="寄存器类型：input / holding"),
    address: int = typer.Option(..., "--address", help="文档地址（将读取 address-1/+0/+1）"),
) -> None:
    """只读 address-1 / address / address+1 三个地址，排查 ±1 地址偏移。"""
    _validate_word_type(register_type)
    if address < 1:
        typer.echo("错误：--address 必须 >= 1（需要读取 address-1）", err=True)
        raise typer.Exit(1)

    try:
        cfg = load_config(config)
        dev_cfg = find_device_config(cfg, device_id)
        registers = asyncio.run(raw_read_registers(dev_cfg, register_type, address - 1, 3))
    except WindHubError as exc:
        typer.echo(f"address-check 失败：{exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"{dev_cfg.device_id}  type={register_type}  文档地址={address}（±1 检查）")
    for reg in registers:
        marker = " <-- 文档地址" if reg.address == address else ""
        typer.echo(f"  address={reg.address}  decimal={reg.value}  hex={reg.value:#06x}{marker}")
