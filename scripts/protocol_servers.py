"""按 Core 配置启动真实 Modbus/ADS/IEC104 虚拟设备，周期更新随机点值。

手工验收：python scripts/protocol_servers.py --config configs/e2e_site --interval 1
自动测试：通过 run_fleet() 上下文共享同一批真实协议服务。

安全原则：配置中的 host/port 就是实际 bind 地址；绝不暗中重映射。默认
仅允许 127.0.0.0/8，若现场配置含非 loopback 地址立即拒绝。ADS 使用
标准 AMS/TCP 48898，不将目标逻辑端口 801/851 当成 TCP 端口。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import logging
import random
import signal
import struct
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import c104
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import ModbusTcpServer
from pyads import constants as ads_constants  # type: ignore[import-untyped]
from pyads.testserver import (  # type: ignore[import-untyped]
    AdsTestServer,
    AdvancedHandler,
    PLCVariable,
)

from core.application.config_snapshot import ConfigSnapshot
from core.domain import DataType, Point
from core.infrastructure.config import YamlConfigAdapter

LOG = logging.getLogger("protocol-servers")
_ADS_TCP_PORT = 48898
_DEFAULT_PORTS = {"modbus": 502, "ads": _ADS_TCP_PORT, "iec104": 2404}
_MODBUS_FC = {"coil": 1, "discrete": 2, "holding": 3, "input": 4}
_MODBUS_WIDTH = {
    "bool": 1, "int8": 1, "uint8": 1, "int16": 1, "uint16": 1,
    "int32": 2, "uint32": 2, "float32": 2,
    "int64": 4, "uint64": 4, "float64": 4,
}


_ADS_FORMAT: dict[DataType, tuple[str, str, str]] = {
    DataType.BOOL: ("ADST_BIT", "BOOL", "?"),
    DataType.INT8: ("ADST_INT8", "SINT", "b"),
    DataType.UINT8: ("ADST_UINT8", "USINT", "B"),
    DataType.INT16: ("ADST_INT16", "INT", "h"),
    DataType.UINT16: ("ADST_UINT16", "UINT", "H"),
    DataType.INT32: ("ADST_INT32", "DINT", "i"),
    DataType.UINT32: ("ADST_UINT32", "UDINT", "I"),
    DataType.INT64: ("ADST_INT64", "LINT", "q"),
    DataType.UINT64: ("ADST_UINT64", "ULINT", "Q"),
    DataType.FLOAT32: ("ADST_REAL32", "REAL", "f"),
    DataType.FLOAT64: ("ADST_REAL64", "LREAL", "d"),
}


def _ads_value(kind: DataType, value: bool | int | float | str) -> bytes:
    if kind is DataType.STRING:
        return str(value).encode("utf-8")[:80].ljust(81, b"\x00")
    return struct.pack("<" + _ADS_FORMAT[kind][2], value)



@dataclass(frozen=True, slots=True)
class EndpointPlan:
    """按设备模型展开的最终协议身份，不修改用户配置。"""

    device_id: str
    protocol: str
    host: str
    port: int
    options: dict[str, Any]
    points: tuple[Point, ...]
    data_types: dict[str, DataType]

    @property
    def address(self) -> tuple[str, int]:
        return self.host, self.port


def plan_devices(
    snapshot: ConfigSnapshot,
    *,
    include_disabled: bool = False,
    allow_nonloopback: bool = False,
) -> list[EndpointPlan]:
    """检查协议、完整点表、TCP bind 冲突及系统绑定安全性。"""
    plans: list[EndpointPlan] = []
    claimed: set[tuple[str, int]] = set()
    for device in snapshot.site.devices.values():
        if not device.enabled and not include_disabled:
            continue
        model = snapshot.device_models[device.device_model_id]
        table = snapshot.point_tables[model.point_table_id]
        family = table.protocol.name
        if family not in _DEFAULT_PORTS:
            raise ValueError(f"{device.device_id}: unsupported protocol {family}")
        host = device.endpoint.host
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError(f"{device.device_id}: bind host must be a literal IP: {host}") from exc
        if address.version != 4 or not allow_nonloopback and not address.is_loopback:
            raise ValueError(
                f"{device.device_id}: {host} is not a local loopback address; "
                "use --allow-nonloopback only when IP is assigned to this host"
            )
        if family == "ads":
            # AMS target_port 851/801 只是 ADS 内部逻辑端口。
            port = _ADS_TCP_PORT
        else:
            port = device.endpoint.port or int(
                model.endpoint_defaults.get("port", _DEFAULT_PORTS[family])
            )
        key = host, port
        if key in claimed:
            raise ValueError(
                f"{device.device_id}: duplicate bind {host}:{port}; "
                "use distinct loopback IP addresses for same-port endpoints"
            )
        claimed.add(key)
        options = dict(model.protocol_options)
        options.update(snapshot.protocol_options_by_device.get(device.device_id, {}))
        points = tuple(table.points.values())
        data_types = {
            point.point_id: snapshot.business_points[point.business_point_id].data_type
            for point in points
        }
        plans.append(
            EndpointPlan(
                str(device.device_id), family, host, port, options, points, data_types
            )
        )
    if not plans:
        raise ValueError("no enabled devices in provided Core configuration")
    for plan in plans:
        validate_points(plan)
    return plans


def validate_points(plan: EndpointPlan) -> None:
    """启动前验证实际服务能力，避免部分端口监听后才遇到不支持的点。"""
    occupied: dict[tuple[object, ...], str] = {}
    for point in plan.points:
        ext = point.ext
        kind = plan.data_types[point.point_id]
        if plan.protocol == "modbus":
            register = str(ext.get("type", ext.get("register_type", ""))).lower()
            if register not in _MODBUS_FC:
                raise ValueError(f"{plan.device_id}.{point.point_id}: unsupported Modbus register type")
            if kind not in _MODBUS_WIDTH:
                raise ValueError(f"{plan.device_id}.{point.point_id}: unsupported Modbus data type")
            offset = int(ext["address"])
            width = _MODBUS_WIDTH[kind]
            if offset < 0 or offset + width > 2046:
                raise ValueError(f"{plan.device_id}.{point.point_id}: Modbus address out of range")
            key = (register, offset)
        elif plan.protocol == "ads":
            if kind is not DataType.STRING and (
                kind not in _ADS_FORMAT
                or not hasattr(ads_constants, _ADS_FORMAT[kind][0])
            ):
                raise ValueError(
                    f"{plan.device_id}.{point.point_id}: unsupported ADS data type {kind}"
                )
            symbol = str(ext.get("symbol", "")).strip()
            if not symbol:
                raise ValueError(f"{plan.device_id}.{point.point_id}: missing ADS symbol")
            key = (symbol,)
        else:
            type_name = str(ext.get("type", "M_ME_NC_1"))
            if not hasattr(c104.Type, type_name):
                raise ValueError(f"{plan.device_id}.{point.point_id}: unknown IEC104 type")
            ioa = int(ext["ioa"])
            if not 1 <= ioa <= 0xFFFFFF:
                raise ValueError(f"{plan.device_id}.{point.point_id}: invalid IEC104 IOA")
            key = (ioa,)
        if key in occupied:
            raise ValueError(
                f"{plan.device_id}: duplicate address {key} for "
                f"{occupied[key]} and {point.point_id}"
            )
        occupied[key] = point.point_id


def random_scalar(kind: DataType, rng: random.Random) -> bool | int | float | str:
    """确定种子时可复现，遵守协议整数溢出限制。"""
    if kind is DataType.BOOL:
        return bool(rng.getrandbits(1))
    if kind is DataType.STRING:
        return f"sim-{rng.randrange(10000):04d}"
    if kind in (DataType.FLOAT32, DataType.FLOAT64):
        return round(rng.uniform(0, 100), 3)
    signed = kind.value.startswith("int")
    bits = int(kind.value.removeprefix("uint" if not signed else "int"))
    max_value = min((1 << (bits - (1 if signed else 0))) - 1, 10000)
    return rng.randrange(max_value + 1)


def _modbus_words(kind: DataType, value: bool | int | float | str) -> list[int]:
    if kind is DataType.BOOL:
        return [int(bool(value))]
    formats = {
        DataType.INT8: "h", DataType.UINT8: "H",
        DataType.INT16: "h", DataType.UINT16: "H",
        DataType.INT32: "i", DataType.UINT32: "I",
        DataType.INT64: "q", DataType.UINT64: "Q",
        DataType.FLOAT32: "f", DataType.FLOAT64: "d",
    }
    if kind not in formats:
        raise ValueError(f"unsupported Modbus type: {kind}")
    encoded = struct.pack(">" + formats[kind], value)
    return list(struct.unpack(">" + "H" * (len(encoded) // 2), encoded))


class _ModbusVirtualServer:
    """使用真实 pymodbus 从站内存块，不占用生产驱动端口。"""

    def __init__(self, plan: EndpointPlan) -> None:
        self.plan = plan
        self.server: ModbusTcpServer | None = None
        self.devices: dict[int, ModbusDeviceContext] = {}

    async def start(self) -> None:
        maximum = 2048
        ids = {int(self.plan.options.get("slave_id", 1))}
        for unit in ids:
            self.devices[unit] = ModbusDeviceContext(
                co=ModbusSequentialDataBlock(1, [0] * maximum),
                di=ModbusSequentialDataBlock(1, [0] * maximum),
                hr=ModbusSequentialDataBlock(1, [0] * maximum),
                ir=ModbusSequentialDataBlock(1, [0] * maximum),
            )
        self.server = ModbusTcpServer(
            ModbusServerContext(self.devices, single=False),
            address=self.plan.address,
        )
        await self.server.serve_forever(background=True)

    def update(self, rng: random.Random) -> None:
        for point in self.plan.points:
            address = point.ext
            register_type = str(address.get("type", address.get("register_type", "")))
            if register_type not in _MODBUS_FC:
                raise ValueError(f"{self.plan.device_id}.{point.point_id}: invalid register type")
            kind = self.plan.data_types[point.point_id]
            register = int(address["address"])
            data = _modbus_words(kind, random_scalar(kind, rng))
            if register < 0 or register + len(data) > 2046:
                raise ValueError(f"{self.plan.device_id}.{point.point_id}: register outside 2048")
            unit = int(self.plan.options.get("slave_id", 1))
            self.devices[unit].setValues(_MODBUS_FC[register_type], register, data)

    async def stop(self) -> None:
        if self.server is not None:
            server, self.server = self.server, None
            await server.shutdown()


class _IEC104VirtualServer:
    """c104 真实 STARTDT、总召及自发上送。"""

    def __init__(self, plan: EndpointPlan) -> None:
        self.plan = plan
        self.server: c104.Server | None = None
        self.points: list[tuple[Any, DataType]] = []

    async def start(self) -> None:
        server = c104.Server(ip=self.plan.host, port=self.plan.port)
        station = server.add_station(common_address=int(self.plan.options.get("common_addr", 1)))
        if station is None:
            raise RuntimeError("cannot create IEC104 station")
        for point in self.plan.points:
            kind = self.plan.data_types[point.point_id]
            type_name = str(point.ext.get("type", "M_ME_NC_1"))
            if not hasattr(c104.Type, type_name):
                raise ValueError(f"{self.plan.device_id}.{point.point_id}: unknown IEC104 type {type_name}")
            instance = station.add_point(
                io_address=int(point.ext["ioa"]), type=getattr(c104.Type, type_name)
            )
            if instance is None:
                raise ValueError(f"IEC104 IOA conflict in {self.plan.device_id}: {point.ext['ioa']}")
            self.points.append((instance, kind))
        self.server = server
        await asyncio.to_thread(server.start)

    def update(self, rng: random.Random) -> None:
        for point, kind in self.points:
            point.value = random_scalar(kind, rng)
            try:
                point.transmit(c104.Cot.SPONTANEOUS)
            except RuntimeError:
                # 无主站连接时周期刷新仍须继续，后续总召可读取最新值。
                LOG.debug("IEC104 spontaneous update has no active client")

    async def stop(self) -> None:
        if self.server is not None:
            server, self.server = self.server, None
            await asyncio.to_thread(server.stop)


class _ADSVirtualServer:
    """pyads testserver 对每个配置 Symbol 提供 AMS/TCP 访问。"""

    def __init__(self, plan: EndpointPlan) -> None:
        self.plan = plan
        self.server: AdsTestServer | None = None
        self.variables: list[tuple[PLCVariable, DataType]] = []

    async def start(self) -> None:
        handler = AdvancedHandler()
        for point in self.plan.points:
            kind = self.plan.data_types[point.point_id]
            symbol = str(point.ext.get("symbol", ""))
            if not symbol:
                raise ValueError(f"{self.plan.device_id}.{point.point_id}: symbol required")
            if kind is DataType.STRING:
                ads_type = ads_constants.ADST_STRING
                symbol_type = "STRING(80)"
            else:
                ads_constant, symbol_type, _ = _ADS_FORMAT[kind]
                ads_type = getattr(ads_constants, ads_constant)
            variable = PLCVariable(
                name=symbol,
                value=_ads_value(kind, random_scalar(kind, random.Random(0))),
                ads_type=ads_type,
                symbol_type=symbol_type,
            )
            handler.add_variable(variable)
            self.variables.append((variable, kind))
        server = AdsTestServer(
            handler=handler,
            ip_address=self.plan.host,
            port=_ADS_TCP_PORT,
            logging=False,
        )
        self.server = server
        server.start()

    def update(self, rng: random.Random) -> None:
        for variable, kind in self.variables:
            variable.write(_ads_value(kind, random_scalar(kind, rng)))

    async def stop(self) -> None:
        if self.server is not None:
            server, self.server = self.server, None
            server.close()
            await asyncio.to_thread(server.join, 5.0)


VirtualServer = _ModbusVirtualServer | _IEC104VirtualServer | _ADSVirtualServer


@asynccontextmanager
async def run_fleet(
    config: str | Path,
    *,
    interval: float = 1.0,
    seed: int | None = None,
    include_disabled: bool = False,
    allow_nonloopback: bool = False,
    failure_event: asyncio.Event | None = None,
) -> AsyncIterator[list[EndpointPlan]]:
    """按配置启动服务并周期更新；任何启动/运行错误均导致整组关闭。"""
    if not 0.01 <= interval <= 3600:
        raise ValueError("interval must be between 0.01 and 3600 seconds")
    snapshot = YamlConfigAdapter(config).load()
    plans = plan_devices(
        snapshot,
        include_disabled=include_disabled,
        allow_nonloopback=allow_nonloopback,
    )
    classes = {
        "modbus": _ModbusVirtualServer,
        "iec104": _IEC104VirtualServer,
        "ads": _ADSVirtualServer,
    }
    started: list[VirtualServer] = []
    update_task: asyncio.Task[None] | None = None
    rng = random.Random(seed)
    try:
        for plan in plans:
            instance = classes[plan.protocol](plan)
            started.append(instance)
            await instance.start()
            LOG.info("started %s %s %s:%d (%d points)", plan.protocol, plan.device_id,
                     plan.host, plan.port, len(plan.points))
        async def update_loop() -> None:
            while True:
                for instance in started:
                    instance.update(rng)
                await asyncio.sleep(interval)
        update_task = asyncio.create_task(update_loop(), name="protocol-random-update")
        if failure_event is not None:
            def mark_failed(task: asyncio.Task[None]) -> None:
                if not task.cancelled() and task.exception() is not None:
                    failure_event.set()

            update_task.add_done_callback(mark_failed)
        # 首轮更新成功后再交给测试/手工验收使用者。
        await asyncio.sleep(0)
        if update_task.done():
            await update_task
        yield plans
    finally:
        failure = None
        if update_task is not None:
            if not update_task.done():
                update_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await update_task
            elif not update_task.cancelled():
                failure = update_task.exception()
        for instance in reversed(started):
            try:
                await instance.stop()
            except Exception:
                LOG.exception("failed to stop virtual server %s", instance.plan.device_id)
        if failure is not None:
            raise failure


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Core site 配置目录")
    parser.add_argument("--interval", type=float, default=1.0, help="数据更新间隔秒数")
    parser.add_argument("--seed", type=int, default=None, help="可复现的随机种子")
    parser.add_argument("--include-disabled", action="store_true", help="也启动禁用设备")
    parser.add_argument("--allow-nonloopback", action="store_true", help="允许绑定已配置的本机非 loopback IP")
    parser.add_argument("--expected-per-protocol", type=int, default=None,
                        help="严格要求三种协议各有 N 个设备（例如 10）")
    parser.add_argument("--check", action="store_true", help="仅校验配置和绑定地址，不启动")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


async def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level))
    snapshot = YamlConfigAdapter(args.config).load()
    plans = plan_devices(
        snapshot, include_disabled=args.include_disabled,
        allow_nonloopback=args.allow_nonloopback,
    )
    groups: dict[str, int] = defaultdict(int)
    for plan in plans:
        groups[plan.protocol] += 1
    LOG.info("configured virtual servers: %s", dict(groups))
    if args.expected_per_protocol is not None:
        if args.expected_per_protocol < 1:
            raise ValueError("--expected-per-protocol must be positive")
        for family in _DEFAULT_PORTS:
            if groups[family] != args.expected_per_protocol:
                raise ValueError(
                    f"{family}: expected {args.expected_per_protocol} endpoints, "
                    f"found {groups[family]}"
                )
    if args.check:
        return 0
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)
    async with run_fleet(
        args.config, interval=args.interval, seed=args.seed,
        include_disabled=args.include_disabled,
        allow_nonloopback=args.allow_nonloopback,
        failure_event=stopping,
    ):
        await stopping.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
