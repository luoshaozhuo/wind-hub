"""Real NATS -> Sink -> IndustrialGatewayService.UpdateVariables link."""

from __future__ import annotations

import asyncio
from typing import Any

import grpc
import pytest

from contracts import industrial_gateway_pb2 as gw_pb
from contracts import industrial_gateway_pb2_grpc as gw_rpc
from core.infrastructure.rpc.scalar_codec import decode_scalar
from tests.support.process import free_port
from tests.support.wait import wait_until
from tests.system.r5.conftest import make_sample, mgmt, publish, wait_state
from tests.system.r5.r5_config import DEVICE_ID, TASK_ID, write_r5_config

pytestmark = [pytest.mark.system, pytest.mark.real_service]


class _RecordingGateway(gw_rpc.IndustrialGatewayServiceServicer):
    """Capture real gRPC UpdateVariables requests and return per-item results."""

    def __init__(self, *, rejected_index: int | None = None, delay: float = 0.0) -> None:
        self.received: list[tuple[str, str, Any]] = []
        self.calls = 0
        self._rejected_index = rejected_index
        self._delay = delay

    async def UpdateVariables(  # noqa: N802
        self, request: gw_pb.UpdateVariablesRequest, context: Any
    ) -> gw_pb.UpdateVariablesResponse:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        for point in request.values:
            self.received.append(
                (
                    request.gateway_id,
                    point.variable_id,
                    decode_scalar(point.value),
                )
            )
        return gw_pb.UpdateVariablesResponse(
            results=[
                gw_pb.VariableUpdateResult(
                    variable_id=point.variable_id,
                    success=index != self._rejected_index,
                    error_code=("INVALID_VALUE" if index == self._rejected_index else ""),
                )
                for index, point in enumerate(request.values)
            ]
        )


async def _start_gateway(
    servicer: _RecordingGateway,
) -> tuple[Any, grpc.aio.Server, int]:
    server = grpc.aio.server()
    gw_rpc.add_IndustrialGatewayServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return servicer, server, port


def _grpc_config(nats_url: str, endpoint: str, *, timeout: float = 3.0) -> dict[str, Any]:
    return {
        "grpc_sink": {
            "type": "grpc",
            "connection": {"endpoint": endpoint, "timeout": timeout},
            "outputs": {
                TASK_ID: {
                    "points": [
                        {
                            "source": {
                                "device_id": DEVICE_ID,
                                "point_id": "rotor_speed",
                            },
                            "address": {"device_id": "gw-1", "point_id": "speed"},
                        },
                        {
                            "source": {"device_id": DEVICE_ID, "point_id": "gen_power"},
                            "address": {"device_id": "gw-1", "point_id": "power"},
                        },
                    ]
                }
            },
        }
    }


async def test_single_and_multi_point_writes_with_scalar_encoding(
    r5_sink_factory, r5_stack, tmp_path
) -> None:
    gateway, server, port = await _start_gateway(_RecordingGateway())
    try:
        config_dir = write_r5_config(
            tmp_path / "cfg",
            free_port(),
            r5_stack.nats,
            _grpc_config(r5_stack.nats, f"127.0.0.1:{port}"),
        )
        sink = await r5_sink_factory(config_dir, realtime_sinks=("grpc_sink",))
        await wait_state(sink, "grpc_sink", "running")

        await publish(
            r5_stack,
            make_sample("rotor_speed", 12.5, sample_id="r5g-1"),
            "realtime",
        )
        await publish(
            r5_stack,
            make_sample("gen_power", 300, sample_id="r5g-2"),
            "realtime",
        )

        async def _both() -> list[tuple[str, str, Any]] | None:
            keys = {(d, p) for d, p, _ in gateway.received}
            if {("gw-1", "speed"), ("gw-1", "power")}.issubset(keys):
                return list(gateway.received)
            return None

        received = await wait_until(
            _both, timeout=20.0, interval=0.2, description="both RPC points"
        )
        by_dest = {(d, p): value for d, p, value in received}
        assert by_dest[("gw-1", "speed")] == pytest.approx(12.5)
        assert by_dest[("gw-1", "power")] == 300
        assert isinstance(by_dest[("gw-1", "power")], int)
    finally:
        await server.stop(grace=1.0)


async def test_partial_write_is_rejected_at_adapter(r5_stack, tmp_path) -> None:
    """Gateway per-item rejection propagates as an adapter failure.

    NATS realtime 每消息一个采样，partial write 不会经消息链路出现，因此
    这里直接驱动生产 GrpcSink.write_task 发真实 gRPC 请求（服务端真实、
    协议真实，仅绕过单样本消息路径）。
    """
    from datetime import UTC, datetime

    from configuration.site_loader import load_domain
    from core.domain.point_value import PointValue
    from sink_service.infrastructure.adapters import GrpcSink
    from sink_service.infrastructure.config import _resolve_sinks

    gateway, server, port = await _start_gateway(_RecordingGateway(rejected_index=1))
    try:
        config_dir = write_r5_config(
            tmp_path / "cfg",
            free_port(),
            r5_stack.nats,
            _grpc_config(r5_stack.nats, f"127.0.0.1:{port}"),
        )
        resolved = _resolve_sinks(load_domain(config_dir))["grpc_sink"]
        adapter = GrpcSink(resolved)
        await adapter.open()
        now = datetime.now(UTC)
        # v1 领域 PointValue 已移除 task_id 字段；任务归属由 write_task 的
        # 显式 task_id 参数承载，DTO 本身只描述设备点值。
        batch = [
            PointValue(
                device_id=DEVICE_ID,
                point_id="rotor_speed",
                value=1.0,
                timestamp=now,
            ),
            PointValue(
                device_id=DEVICE_ID,
                point_id="gen_power",
                value=2.0,
                timestamp=now,
            ),
        ]
        with pytest.raises(RuntimeError, match="INVALID_VALUE"):
            await adapter.write_task(TASK_ID, batch)
        # Both items reached the real service; second item was rejected.
        assert gateway.calls >= 1
        assert len(gateway.received) == 2
        await adapter.close()
    finally:
        await server.stop(grace=1.0)


async def test_rpc_timeout_propagates_and_recovers_after_peer_restart(
    r5_sink_factory, r5_stack, tmp_path
) -> None:
    slow, slow_server, port = await _start_gateway(_RecordingGateway(delay=5.0))
    try:
        config_dir = write_r5_config(
            tmp_path / "cfg",
            free_port(),
            r5_stack.nats,
            _grpc_config(r5_stack.nats, f"127.0.0.1:{port}", timeout=1.0),
        )
        sink = await r5_sink_factory(config_dir, realtime_sinks=("grpc_sink",))
        await wait_state(sink, "grpc_sink", "running")

        await publish(
            r5_stack,
            make_sample("rotor_speed", 8.0, sample_id="r5g-slow"),
            "realtime",
        )

        async def _failed() -> dict[str, Any] | None:
            status = await mgmt(sink, "GetSinkStatus", "grpc_sink")
            if status["state"] == "failed" and status["failure"]:
                return status
            return None

        failed = await wait_until(
            _failed, timeout=15.0, interval=0.25, description="timeout failed"
        )
        assert (
            "deadline" in str(failed["failure"]).lower()
            or "timeout" in str(failed["failure"]).lower()
        )
    finally:
        await slow_server.stop(grace=0.0)

    # 对端在同一端口以正常行为重启：supervisor 每 5s 自动重启 FAILED Sink，
    # 新通道续写成功。
    server2 = grpc.aio.server()
    gw2 = _RecordingGateway()
    gw_rpc.add_IndustrialGatewayServiceServicer_to_server(gw2, server2)
    bound = server2.add_insecure_port(f"127.0.0.1:{port}")
    assert bound == port
    await server2.start()
    try:
        await wait_state(sink, "grpc_sink", "running", timeout=60.0)
        await publish(
            r5_stack,
            make_sample("rotor_speed", 9.0, sample_id="r5g-recovered"),
            "realtime",
        )

        async def _recovered() -> tuple[str, str, Any] | None:
            for device, point, value in gw2.received:
                if (device, point) == ("gw-1", "speed") and value == pytest.approx(9.0):
                    return device, point, value
            return None

        await wait_until(
            _recovered,
            timeout=30.0,
            interval=0.25,
            description="post-restart write",
        )
        status = await mgmt(sink, "GetSinkStatus", "grpc_sink")
        assert status["failure"] is None
    finally:
        await server2.stop(grace=1.0)
