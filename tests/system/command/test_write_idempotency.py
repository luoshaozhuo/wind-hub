"""System E2E：命令幂等验收——同一 command_id 的并发/重复写只允许一次到达设备。

写命令链路在现行架构中归属 Commander：客户端经真实 gRPC socket 调用
Commander subprocess 的 ``WritePoint``（wire contract 支持调用方携带
``command_id``）。设备侧效果以 Modbus 从站写请求计数 + 独立客户端回读
佐证——全部断言都在系统边界上，不读取进程内部状态。
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import grpc
import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.config_helper import write_config_tree
from tests.support.process import CollectorProcess
from wind_hub_core.rpc import commander_pb2 as pb
from wind_hub_core.rpc import commander_pb2_grpc as pb_grpc

pytestmark = pytest.mark.modbus

#: 与 ModbusMockServer 默认寄存器布局一致的点表（setpoint.power @ holding 200）。
_COMMANDER_POINTS: list[dict[str, object]] = [
    {
        "point_id": "setpoint.power",
        "point_groups": ["control"],
        "address": {"register_type": "holding", "address": 200},
        "data_type": "float32",
    },
]


def _decode_float32(registers: list[int]) -> float:
    """按 big-endian float32 解码两个 16 位寄存器。"""
    return struct.unpack(">f", struct.pack(">HH", registers[0], registers[1]))[0]


async def _write_point_rpc(
    target: str,
    *,
    command_id: str,
    device_id: str = "modbus-1",
    point_id: str = "setpoint.power",
    value: float,
) -> pb.CommandResultMessage:
    """经真实 socket 发送一次携带 command_id 的 WritePoint RPC。"""
    channel = grpc.aio.insecure_channel(target)
    try:
        stub = pb_grpc.CommanderServiceStub(channel)
        return await stub.WritePoint(
            pb.WritePointRequest(
                command_id=command_id,
                device_id=device_id,
                point_id=point_id,
                value=pb.ScalarValue(double_value=value),
                timeout=5.0,
            ),
            timeout=10.0,
        )
    finally:
        await channel.close()


@pytest.fixture
async def idem_env(
    modbus_server: ModbusMockServer,
    commander_factory,
    tmp_path: Path,
) -> tuple[CollectorProcess, ModbusMockServer]:
    """幂等验收环境：真实从站 + Commander subprocess。"""
    config_dir = write_config_tree(
        tmp_path / "cfg",
        devices=[
            {
                "device_id": "modbus-1",
                "protocol": "modbus",
                "point_table": "modbus",
                "endpoint": {
                    "host": "127.0.0.1",
                    "port": modbus_server.port,
                    "extensions": {"unit_id": 1, "timeout": 2.0, "word_order": "big_endian"},
                },
            }
        ],
        point_tables={"modbus": {"protocol": "modbus", "points": list(_COMMANDER_POINTS)}},
        tasks=[],
        sinks=[],
        system={"runtime": {"connect_timeout": 2.0, "write_timeout": 2.0}},
    )
    proc: CollectorProcess = await commander_factory(config_dir)
    modbus_server.reset_write_count()
    return proc, modbus_server


class TestWriteIdempotency:
    async def test_concurrent_same_command_id_executes_once(
        self, idem_env: tuple[CollectorProcess, ModbusMockServer]
    ) -> None:
        proc, server = idem_env
        command_id = "cmd-system-idem-concurrent"

        responses = await asyncio.gather(
            *(
                _write_point_rpc(proc.grpc_target, command_id=command_id, value=66.6)
                for _ in range(8)
            )
        )

        assert all(r.success for r in responses)
        assert all(r.command_id == command_id for r in responses)
        # 8 个并发同 id 请求共享一次执行——从站只收到一个写请求。
        assert server.write_count == 1
        registers = await server.read_holding(1, 200, 2)
        assert _decode_float32(registers) == pytest.approx(66.6)

    async def test_repeated_command_id_returns_cached_result(
        self, idem_env: tuple[CollectorProcess, ModbusMockServer]
    ) -> None:
        proc, server = idem_env

        first = await _write_point_rpc(
            proc.grpc_target, command_id="cmd-system-idem-cached", value=77.7
        )
        assert first.success
        assert server.write_count == 1

        # 完成后的同 id 重复请求命中结果缓存——不再触发设备写。
        repeat = await _write_point_rpc(
            proc.grpc_target, command_id="cmd-system-idem-cached", value=77.7
        )
        assert repeat.success
        assert repeat.command_id == "cmd-system-idem-cached"
        assert server.write_count == 1

        # 不同 command_id 是新的命令——必须真正下发。
        other = await _write_point_rpc(
            proc.grpc_target, command_id="cmd-system-idem-other", value=88.8
        )
        assert other.success
        assert server.write_count == 2
        registers = await server.read_holding(1, 200, 2)
        assert _decode_float32(registers) == pytest.approx(88.8)

    async def test_empty_command_id_gets_server_generated_id(
        self, idem_env: tuple[CollectorProcess, ModbusMockServer]
    ) -> None:
        """调用方不带 command_id 时 Commander 生成幂等标识并回填响应。"""
        proc, server = idem_env
        result = await _write_point_rpc(proc.grpc_target, command_id="", value=55.5)
        assert result.success
        assert result.command_id != ""
        assert server.write_count == 1
