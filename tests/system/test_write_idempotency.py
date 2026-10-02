"""System E2E：命令幂等验收——同一 command_id 的并发/重复写只允许一次到达设备。

客户端经真实 gRPC socket 直连 Collector subprocess 的 WritePoint（wire
contract 支持调用方携带 ``command_id``；ctl CLI 不暴露该参数，故这里用
原生 Struct RPC——仍是系统边界调用，不读取进程内部状态）。设备侧效果
以 Modbus 从站写请求计数 + 独立客户端回读佐证。
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from typing import Any

import grpc
import pytest
from google.protobuf import json_format, struct_pb2
from wind_hub_core.rpc.collector import CONTROL_SERVICE, WRITE_POINT, rpc_path

from tests.collector.functional.conftest import write_functional_config
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.system.process import CollectorProcess

pytestmark = pytest.mark.modbus


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
) -> dict[str, Any]:
    """经真实 socket 发送一次携带 command_id 的 WritePoint RPC。"""
    request = struct_pb2.Struct()
    json_format.ParseDict(
        {
            "command_id": command_id,
            "device_id": device_id,
            "point_id": point_id,
            "value": value,
            "timeout": 5.0,
        },
        request,
    )
    channel = grpc.aio.insecure_channel(target)
    try:
        call = channel.unary_unary(
            rpc_path(CONTROL_SERVICE, WRITE_POINT),
            request_serializer=struct_pb2.Struct.SerializeToString,
            response_deserializer=struct_pb2.Struct.FromString,
        )
        response = await call(request, timeout=10.0)
        return json_format.MessageToDict(response, preserving_proto_field_name=True)
    finally:
        await channel.close()


@pytest.fixture
async def idem_env(
    modbus_server: ModbusMockServer,
    collector_factory,
    tmp_path: Path,
) -> tuple[CollectorProcess, ModbusMockServer]:
    """幂等验收环境：真实从站 + Collector subprocess（File sink 兜底任务目标）。"""
    config_dir = write_functional_config(
        tmp_path / "cfg",
        modbus_server.port,
        sinks=[
            {
                "name": "file_sink",
                "type": "file",
                "params": {"path": str(tmp_path / "out" / "telemetry.jsonl")},
            }
        ],
        tasks=[
            {
                "task_id": "modbus-telemetry",
                "device": "modbus-1",
                "point_group": "telemetry",
                "interval": 0.2,
                "targets": [{"sink": "file_sink"}],
            }
        ],
    )
    proc: CollectorProcess = await collector_factory(config_dir)
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

        assert all(r["success"] is True for r in responses)
        assert all(r["command_id"] == command_id for r in responses)
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
        assert first["success"] is True
        assert server.write_count == 1

        # 完成后的同 id 重复请求命中结果缓存——不再触发设备写。
        repeat = await _write_point_rpc(
            proc.grpc_target, command_id="cmd-system-idem-cached", value=77.7
        )
        assert repeat["success"] is True
        assert repeat["command_id"] == "cmd-system-idem-cached"
        assert server.write_count == 1

        # 不同 command_id 是新的命令——必须真正下发。
        other = await _write_point_rpc(
            proc.grpc_target, command_id="cmd-system-idem-other", value=88.8
        )
        assert other["success"] is True
        assert server.write_count == 2
        registers = await server.read_holding(1, 200, 2)
        assert _decode_float32(registers) == pytest.approx(88.8)
