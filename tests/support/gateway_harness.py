"""industrial_gateway 真实链路测试支撑。

- 真实 ``PymodbusGatewayServer``（pymodbus 3.15 simulator 协议栈，不解析协议）；
- 真实 pymodbus ``AsyncModbusTcpClient``，通过 ``source_address`` 模拟
  Collector(127.0.0.2) / DC-EMS(127.0.0.3) / 未授权方(127.0.0.1) 三种身份；
- 真实 grpc.aio Commander 服务端，可逐点配置三态、延迟与并发观测。

唯一的测试期垫片见 ``install_server_shims``：生产代码
``server.PymodbusGatewayServer`` 对空 coil/discrete 块填入
``DataType.INVALID`` 占位，pymodbus 3.15.0 对 bit 块强制要求
``DataType.BITS``，导致任何合法配置都无法构造 SimDevice（出厂配置同样无法
启动）。垫片仅把该非法占位替换为等价的 BITS 占位，不改变任何业务路径。
"""

from __future__ import annotations

import asyncio
import socket
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import grpc
import pytest
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.simulator import DataType, SimData, SimDevice

from core.infrastructure.rpc import commander_pb2 as pb
from core.infrastructure.rpc import commander_pb2_grpc
from industrial_gateway.assembly import assemble_gateway
from industrial_gateway.infrastructure.config.loader import GatewayConfig
from industrial_gateway.infrastructure.protocol.modbus import server as gw_server

COLLECTOR_IP = "127.0.0.2"
COMMANDER_IP = "127.0.0.3"
UNAUTHORIZED_IP = "127.0.0.1"
SHIM_BIT_ADDRESS = 0xFFF0  # 测试不访问的高位 bit 地址（bit 编号）

FEEDBACK_FLOAT = 100
FEEDBACK_PAIR = 102
FEEDBACK_UINT = 110
COMMAND_FLOAT = 200
COMMAND_UINT = 210
COIL_FB_0 = 0
COIL_FB_1 = 1
COIL_CMD = 16


def reserve_port() -> int:
    """预留一个本机 TCP 端口（存在极小竞态，测试环境足够）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def install_server_shims(monkeypatch: pytest.MonkeyPatch, *, multidrop: bool = True) -> None:
    """安装测试期垫片，分别对应生产缺陷，均不改变业务路径。

    缺陷 A（启动即崩）：生产代码对空 coil/discrete 块填入
    ``DataType.INVALID`` 占位，pymodbus 3.15.0 对 bit 块强制要求
    ``DataType.BITS``，任何配置（含出厂配置）都无法构造 SimDevice。
    垫片把该非法占位替换为等价的高位 BITS 占位。

    缺陷 B（所有 TCP 连接被 reset）：生产代码无条件置
    ``server.allow_multiple_devices = True``，而 pymodbus 3.15.0 的
    ``ServerRequestHandler.__init__`` 随后调用 socket framer 不存在的
    ``setMultidrop``（仅 RTU framer 实现），连接工厂抛 AttributeError，
    客户端收不到任何 Modbus 响应。垫片给 socket framer 补 no-op
    setMultidrop（TCP 的 MBAP 头已显式携带 Unit ID，SimCore 按 dev_id
    路由，本就不需要 RTU 式线路过滤）。``multidrop=False`` 时不安装该
    垫片，用于缺陷 B 的复现测试。
    """
    from pymodbus.framer.socket import FramerSocket

    if multidrop and not hasattr(FramerSocket, "setMultidrop"):
        monkeypatch.setattr(
            FramerSocket,
            "setMultidrop",
            lambda self, device_ids: None,
            raising=False,
        )

    class ShimSimDevice(SimDevice):
        def __init__(self, *, id: int, simdata: object, action: object = None) -> None:
            if isinstance(simdata, tuple):
                blocks: list[list[SimData]] = []
                for index, block in enumerate(simdata):
                    block = list(block)
                    if index < 2 and len(block) == 1 and block[0].datatype == DataType.INVALID:
                        block = [
                            SimData(
                                address=SHIM_BIT_ADDRESS,
                                values=False,
                                datatype=DataType.BITS,
                            )
                        ]
                    blocks.append(block)
                simdata = tuple(blocks)
            super().__init__(id=id, simdata=simdata, action=action)  # type: ignore[arg-type]

    monkeypatch.setattr(gw_server, "SimDevice", ShimSimDevice)


def float_words(value: float) -> list[int]:
    return list(struct.unpack(">HH", struct.pack(">f", value)))


def words_float(words: Sequence[int]) -> float:
    return struct.unpack(">f", struct.pack(">HH", *words))[0]


@dataclass(slots=True)
class CommanderCall:
    device_id: str
    point_id: str
    command_id: str
    kind: str
    value: bool | int | float
    timeout: float


@dataclass(slots=True)
class _Gate:
    """逐点串行闸门：观测并强制 Commander 现场执行串行化。"""

    inflight: dict[str, int] = field(default_factory=dict)
    max_inflight: dict[str, int] = field(default_factory=dict)


class FakeCommanderService(commander_pb2_grpc.CommanderServiceServicer):
    """真实 grpc.aio 服务端：按 (device, point) 配置三态与延迟。"""

    def __init__(self) -> None:
        self.calls: list[CommanderCall] = []
        self.outcomes: dict[tuple[str, str], int] = {}
        self.delays: dict[tuple[str, str], float] = {}
        self._gate = _Gate()

    def set_outcome(self, physical_device: str, point: str, outcome: int) -> None:
        self.outcomes[(physical_device, point)] = outcome

    def set_delay(self, physical_device: str, point: str, seconds: float) -> None:
        self.delays[(physical_device, point)] = seconds

    def max_inflight(self, physical_device: str, point: str) -> int:
        return self._gate.max_inflight.get(f"{physical_device}.{point}", 0)

    async def WritePoint(  # noqa: N802 - 方法名由 proto service 定义强制
        self, request: pb.WritePointRequest, context: object
    ) -> pb.CommandResultMessage:
        kind = request.value.WhichOneof("kind")
        assert kind is not None
        raw: bool | int | float = getattr(request.value, kind)
        key = (request.device_id, request.point_id)
        gate_key = f"{request.device_id}.{request.point_id}"
        current = self._gate.inflight.get(gate_key, 0) + 1
        self._gate.inflight[gate_key] = current
        self._gate.max_inflight[gate_key] = max(current, self._gate.max_inflight.get(gate_key, 0))
        self.calls.append(
            CommanderCall(
                request.device_id,
                request.point_id,
                request.command_id,
                kind,
                raw,
                request.timeout,
            )
        )
        try:
            delay = self.delays.get(key, 0.0)
            if delay:
                await asyncio.sleep(delay)
            outcome = self.outcomes.get(key, pb.COMMAND_OUTCOME_CONFIRMED_SUCCESS)
            return pb.CommandResultMessage(command_id=request.command_id, outcome=outcome)
        finally:
            self._gate.inflight[gate_key] -= 1


def build_config(
    port: int,
    commander_port: int,
    journal_path: Path,
    *,
    units: Sequence[int] | None = None,
    data_writers: Sequence[str] = (COLLECTOR_IP,),
    command_writers: Sequence[str] = (COMMANDER_IP,),
    command_timeout: float = 3.0,
    rpc_timeout: float = 4.0,
    max_age_seconds: float = 30.0,
    control_enabled: bool = True,
) -> GatewayConfig:
    unit_ids = list(range(1, 13)) if units is None else list(units)
    devices = {
        f"turbine_{unit:03d}": {
            "endpoint_id": "modbus_main",
            "point_table_id": "turbine_modbus",
            "protocol_address": {"unit_id": unit},
            "commands": {
                "cmd_float": {"device_id": f"phys{unit}", "point_id": "sp"},
                "cmd_uint": {"device_id": f"phys{unit}", "point_id": "spu"},
                "cmd_coil": {"device_id": f"phys{unit}", "point_id": "spb"},
            },
        }
        for unit in unit_ids
    }
    config: dict[str, object] = {
        "endpoints": {
            "modbus_main": {
                "protocol": "modbus",
                "host": "127.0.0.1",
                "port": port,
                "protocol_options": {"mode": "tcp"},
            }
        },
        "point_tables": {
            "turbine_modbus": {
                "protocol": "modbus",
                "points": {
                    "fb_float": {
                        "role": "feedback",
                        "data_type": "float32",
                        "address": {
                            "register_type": "holding",
                            "address": FEEDBACK_FLOAT,
                            "word_order": "big_endian",
                        },
                        "max_age_seconds": max_age_seconds,
                    },
                    "fb_pair": {
                        "role": "feedback",
                        "data_type": "uint16",
                        "address": {
                            "register_type": "holding",
                            "address": FEEDBACK_PAIR,
                        },
                    },
                    "fb_uint": {
                        "role": "feedback",
                        "data_type": "uint16",
                        "address": {
                            "register_type": "holding",
                            "address": FEEDBACK_UINT,
                        },
                    },
                    "fb_coil_0": {
                        "role": "feedback",
                        "data_type": "bool",
                        "address": {"register_type": "coil", "address": COIL_FB_0},
                    },
                    "fb_coil_1": {
                        "role": "feedback",
                        "data_type": "bool",
                        "address": {"register_type": "coil", "address": COIL_FB_1},
                    },
                    "cmd_float": {
                        "role": "command",
                        "data_type": "float32",
                        "address": {
                            "register_type": "holding",
                            "address": COMMAND_FLOAT,
                            "word_order": "big_endian",
                        },
                    },
                    "cmd_uint": {
                        "role": "command",
                        "data_type": "uint16",
                        "address": {
                            "register_type": "holding",
                            "address": COMMAND_UINT,
                        },
                    },
                    "cmd_coil": {
                        "role": "command",
                        "data_type": "bool",
                        "address": {"register_type": "coil", "address": COIL_CMD},
                    },
                },
            }
        },
        "devices": devices,
        "control": {
            "enabled": control_enabled,
            "commander": {
                "endpoint": f"127.0.0.1:{commander_port}",
                "command_timeout": command_timeout,
                "rpc_timeout": rpc_timeout,
            },
            "journal_path": str(journal_path),
            "access": {
                "data_writers": list(data_writers),
                "command_writers": list(command_writers),
            },
        },
    }
    return GatewayConfig.model_validate(config)


@dataclass(slots=True)
class GatewayHarness:
    modbus_port: int
    commander_port: int
    commander: FakeCommanderService
    grpc_server: grpc.aio.Server
    application: object
    journal_path: Path

    async def client(self, source_ip: str) -> AsyncModbusTcpClient:
        client = AsyncModbusTcpClient(
            "127.0.0.1",
            port=self.modbus_port,
            source_address=(source_ip, 0),
            timeout=5,
        )
        await client.connect()
        assert client.connected
        return client

    async def stop(self) -> None:
        await self.application.stop()  # type: ignore[attr-defined]
        await self.grpc_server.stop(grace=0)


async def start_gateway(config: GatewayConfig, commander: FakeCommanderService) -> GatewayHarness:
    commander_port = int(config.control.commander.endpoint.rsplit(":", 1)[1])
    grpc_server = grpc.aio.server()
    commander_pb2_grpc.add_CommanderServiceServicer_to_server(commander, grpc_server)
    grpc_server.add_insecure_port(f"127.0.0.1:{commander_port}")
    await grpc_server.start()

    modbus_port = config.endpoints["modbus_main"].port
    application = assemble_gateway(config)
    await application.start()
    journal_path = Path(config.control.journal_path)
    return GatewayHarness(
        modbus_port=modbus_port,
        commander_port=commander_port,
        commander=commander,
        grpc_server=grpc_server,
        application=application,
        journal_path=journal_path,
    )
