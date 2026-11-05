"""真实子进程：ClaimTargets 租约为纯内存态，崩溃重启后全部失效。

生产设计（forwarder/lease 无持久化）的关键语义：进程退出不闭锁后续接管。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import grpc
import pytest

from contracts import industrial_gateway_pb2 as pb
from contracts import industrial_gateway_pb2_grpc as pb_rpc
from core.infrastructure.rpc.scalar_codec import encode_scalar
from tests.support.gateway_v1 import (
    FEEDBACK_FLOAT,
    ServerSpec,
    reserve_port,
    write_config_tree,
)

pytestmark = [pytest.mark.system, pytest.mark.real_service]


async def _wait_port_open(port: int, timeout: float = 8.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.5
            )
            writer.close()
            return
        except (TimeoutError, ConnectionError, OSError):
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.1)


def _spawn(service_path: Path, root: Path) -> asyncio.subprocess.Process:
    return asyncio.create_subprocess_exec(
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


async def test_claims_do_not_survive_process_restart(tmp_path: Path) -> None:
    root = tmp_path / "root"
    grpc_port = reserve_port()
    service_path = write_config_tree(
        root,
        [
            ServerSpec(
                server_id="gw1",
                device_id="D01",
                modbus_port=reserve_port(),
                control_enabled=False,
                command_mappings=(),
            )
        ],
        commander_endpoint=f"127.0.0.1:{reserve_port()}",
        grpc_port=grpc_port,
    )

    proc = await _spawn(service_path, root)
    token = ""
    try:
        await _wait_port_open(grpc_port)
        async with grpc.aio.insecure_channel(f"127.0.0.1:{grpc_port}") as channel:
            stub = pb_rpc.IndustrialGatewayServiceStub(channel)
            claim = await stub.ClaimTargets(
                pb.ClaimTargetsRequest(
                    gateway_id="gw1",
                    owner_id="A",
                    variable_ids=[FEEDBACK_FLOAT],
                    action="ACQUIRE",
                    lease_ms=60000,
                )
            )
            assert claim.accepted
            token = claim.lease_token
        proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    restarted = await _spawn(service_path, root)
    try:
        await _wait_port_open(grpc_port)
        async with grpc.aio.insecure_channel(f"127.0.0.1:{grpc_port}") as channel:
            stub = pb_rpc.IndustrialGatewayServiceStub(channel)
            # 旧 token 不再 fencing：任意新 owner 可立即占用。
            takeover = await stub.ClaimTargets(
                pb.ClaimTargetsRequest(
                    gateway_id="gw1",
                    owner_id="B",
                    variable_ids=[FEEDBACK_FLOAT],
                    action="ACQUIRE",
                    lease_ms=60000,
                )
            )
            assert takeover.accepted
            # 旧 owner 用旧 token 写入被拒（进程内无此租约）。
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.UpdateVariables(
                    pb.UpdateVariablesRequest(
                        gateway_id="gw1",
                        lease_token=token,
                        values=[
                            pb.VariableUpdate(
                                variable_id=FEEDBACK_FLOAT,
                                value=encode_scalar(1.0),
                            )
                        ],
                    )
                )
            assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    finally:
        if restarted.returncode is None:
            restarted.kill()
            await restarted.wait()
