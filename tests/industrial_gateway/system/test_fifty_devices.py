"""50 台设备 = 50 个独立 Modbus TCP 监听器的真实链路扇出测试。

v1 用"一台设备一个 Server/端口/Unit"取代 v2 单连接多 unit 多维rop；
本文件验证 50 个独立监听器在同一进程（及真实子进程）中并发服务、
逐设备哨兵值零串扰、gRPC 面 50 个网关可见。
"""

from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path

import grpc
import pytest

from contracts import industrial_gateway_pb2 as pb
from contracts import industrial_gateway_pb2_grpc as gw_rpc
from contracts.scalar_codec import encode_scalar
from tests.support.gateway_v1 import (
    FEEDBACK_FLOAT,
    POINT_TABLE,
    ServerSpec,
    claim_for_injection,
    reserve_port,
    start_gateway_v1,
    write_config_tree,
)

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.modbus]

DEVICE_COUNT = 50
FLOAT_ADDR = POINT_TABLE[FEEDBACK_FLOAT]["address"]["address"]


def _specs() -> list[ServerSpec]:
    return [
        ServerSpec(
            server_id=f"gw{index:02d}",
            device_id=f"D{index:03d}",
            modbus_port=reserve_port(),
            slave_id=(index % 247) + 1,
            control_enabled=False,
            command_mappings=(),
        )
        for index in range(DEVICE_COUNT)
    ]


def _float_words(value: float) -> tuple[int, int]:
    return struct.unpack(">HH", struct.pack(">f", value))


async def _fc03_float(port: int, unit: int) -> float:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        pdu = struct.pack(">BHH", 0x03, FLOAT_ADDR, 2)
        writer.write(struct.pack(">HHHB", 1, 0, len(pdu) + 1, unit) + pdu)
        header = await reader.readexactly(7)
        _tid, _proto, length, _unit = struct.unpack(">HHHB", header)
        body = await reader.readexactly(length - 1)
        words = struct.unpack(">HH", body[2:6])
        return struct.unpack(">f", struct.pack(">HH", *words))[0]
    finally:
        writer.close()


async def test_fifty_independent_servers_in_process(tmp_path: Path) -> None:
    specs = _specs()
    gateway = await start_gateway_v1(tmp_path / "root", specs)
    try:
        listed = await gateway.gateway_stub().ListGateways(pb.ListGatewaysRequest(page_size=100))
        assert len(listed.gateways) == DEVICE_COUNT

        # 每设备唯一哨兵：index*7+1/3，float32 可精确保存的小整数。
        sentinels = {spec.server_id: float(index * 7 + 1) for index, spec in enumerate(specs)}

        async def seed_and_read(spec: ServerSpec) -> tuple[str, float, float]:
            value = sentinels[spec.server_id]
            token = await claim_for_injection(
                gateway.gateway_stub(), spec.server_id, [FEEDBACK_FLOAT]
            )
            reply = await gateway.gateway_stub().UpdateVariables(
                pb.UpdateVariablesRequest(
                    gateway_id=spec.server_id,
                    lease_token=token,
                    values=[
                        pb.VariableUpdate(variable_id=FEEDBACK_FLOAT, value=encode_scalar(value))
                    ],
                )
            )
            assert reply.results[0].success
            wire = await _fc03_float(spec.modbus_port, spec.slave_id)
            return spec.server_id, value, wire

        results = await asyncio.gather(*(seed_and_read(spec) for spec in specs))
        for server_id, seeded, wire in results:
            assert wire == pytest.approx(seeded), f"{server_id}: {wire} != {seeded}"
    finally:
        await gateway.stop()


async def test_fifty_independent_servers_real_subprocess(tmp_path: Path) -> None:
    """真实 ``python -m industrial_gateway.main`` 子进程承载 50 监听器。"""
    root = tmp_path / "root"
    specs = _specs()
    grpc_port = reserve_port()
    commander_port = reserve_port()
    service_path = write_config_tree(
        root,
        specs,
        commander_endpoint=f"127.0.0.1:{commander_port}",
        grpc_port=grpc_port,
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "industrial_gateway.main",
        "--config",
        str(service_path),
        "--project-root",
        str(root),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    proc_exited_during_startup = False
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{grpc_port}") as channel:
            gw_stub = gw_rpc.IndustrialGatewayServiceStub(channel)

            # 控制面先确认 50 个网关全部注册（数据面初始化完成的权威信号），
            # 避免 50 个端口探测在冷启动时形成连接雷群。
            control_deadline = asyncio.get_running_loop().time() + 45.0
            registered = 0
            while asyncio.get_running_loop().time() < control_deadline:
                if proc.returncode is not None:
                    proc_exited_during_startup = True
                    break
                try:
                    listed = await gw_stub.ListGateways(pb.ListGatewaysRequest(page_size=100))
                    registered = len(listed.gateways)
                    if registered == DEVICE_COUNT:
                        break
                except grpc.aio.AioRpcError:
                    pass
                await asyncio.sleep(0.1)
            assert not proc_exited_during_startup, "gateway subprocess exited during startup"
            assert registered == DEVICE_COUNT, f"only {registered}/50 gateways registered"

            # 50 个数据面端口逐一可连。
            port_deadline = asyncio.get_running_loop().time() + 15.0

            async def wait_port(port: int) -> None:
                while asyncio.get_running_loop().time() < port_deadline:
                    try:
                        _, writer = await asyncio.open_connection("127.0.0.1", port)
                        writer.close()
                        return
                    except OSError:
                        await asyncio.sleep(0.05)
                raise AssertionError(f"port {port} never opened")

            await asyncio.gather(*(wait_port(spec.modbus_port) for spec in specs))

            # 50 路并发 FC03：未初始化反馈点必须全部读到 0.0（无串扰/无错连）。
            # 冷启动竞态下给有界重读；断言标准不降——每一轮都必须 50 路全零。
            values: list[float] = []
            for _ in range(5):
                values = await asyncio.gather(
                    *(_fc03_float(spec.modbus_port, spec.slave_id) for spec in specs)
                )
                if values == pytest.approx([0.0] * DEVICE_COUNT):
                    break
                await asyncio.sleep(0.3)
            assert values == pytest.approx([0.0] * DEVICE_COUNT)

        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=10.0)
        assert proc.returncode == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
