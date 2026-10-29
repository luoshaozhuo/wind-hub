"""R5-4：NATS → Sink Service → gRPC Gateway 下行真实链路。

测试侧实现真实的 ``GatewayDataService`` gRPC 服务（loopback 动态端口），
生产 ``GrpcSink`` 作为客户端发起 WriteMany。覆盖：单/多点写与标量编码、
部分写（updated < 请求点数）必须拒绝并使 Sink failed、RPC 超时传播、
对端重启后 supervisor 恢复续写。
"""

from __future__ import annotations

import asyncio
from typing import Any

import grpc
import pytest

from contracts import gateway_pb2 as gw_pb
from contracts import gateway_pb2_grpc as gw_rpc
from core.infrastructure.rpc.scalar_codec import decode_scalar
from tests.support.process import free_port
from tests.support.wait import wait_until
from tests.system.r5.conftest import make_sample, mgmt, publish, wait_state
from tests.system.r5.r5_config import DEVICE_ID, TASK_ID, write_r5_config

pytestmark = [pytest.mark.system, pytest.mark.real_service]


class _RecordingGateway(gw_rpc.GatewayDataServiceServicer):
    """真实 gRPC 服务端：记录每次 WriteMany 的解码后标量。"""

    def __init__(self, *, updated_override: int | None = None, delay: float = 0.0) -> None:
        self.received: list[tuple[str, str, Any]] = []
        self.calls = 0
        self._updated_override = updated_override
        self._delay = delay

    async def WriteMany(  # noqa: N802 - 方法名由 protobuf servicer 契约强制
        self, request: gw_pb.WriteManyRequest, context: Any
    ) -> gw_pb.WriteManyResponse:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        for point in request.points:
            self.received.append((point.device_id, point.point_id, decode_scalar(point.value)))
        updated = len(request.points) if self._updated_override is None else self._updated_override
        return gw_pb.WriteManyResponse(updated=updated)


async def _start_gateway(servicer: _RecordingGateway) -> tuple[Any, grpc.aio.Server, int]:
    server = grpc.aio.server()
    gw_rpc.add_GatewayDataServiceServicer_to_server(servicer, server)
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
                            "source": {"device_id": DEVICE_ID, "point_id": "rotor_speed"},
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

        await publish(r5_stack, make_sample("rotor_speed", 12.5, sample_id="r5g-1"), "realtime")
        await publish(r5_stack, make_sample("gen_power", 300, sample_id="r5g-2"), "realtime")

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
    """多点同批 WriteMany 只确认部分时，生产 adapter 必须整体拒绝。

    NATS realtime 每消息一个采样，partial write 不会经消息链路出现，因此
    这里直接驱动生产 GrpcSink.write_task 发真实 gRPC 请求（服务端真实、
    协议真实，仅绕过单样本消息路径）。
    """
    from datetime import UTC, datetime

    from core.domain.point_value import PointValue
    from core.infrastructure.config import YamlConfigAdapter
    from sink_service.infrastructure.adapters import GrpcSink
    from sink_service.infrastructure.config import _resolve_sinks

    gateway, server, port = await _start_gateway(_RecordingGateway(updated_override=1))
    try:
        config_dir = write_r5_config(
            tmp_path / "cfg",
            free_port(),
            r5_stack.nats,
            _grpc_config(r5_stack.nats, f"127.0.0.1:{port}"),
        )
        resolved = _resolve_sinks(YamlConfigAdapter(config_dir).load())["grpc_sink"]
        adapter = GrpcSink(resolved)
        await adapter.open()
        now = datetime.now(UTC)
        batch = [
            PointValue(
                device_id=DEVICE_ID,
                point_id="rotor_speed",
                value=1.0,
                timestamp=now,
                task_id=TASK_ID,
            ),
            PointValue(
                device_id=DEVICE_ID,
                point_id="gen_power",
                value=2.0,
                timestamp=now,
                task_id=TASK_ID,
            ),
        ]
        with pytest.raises(RuntimeError, match="partial write"):
            await adapter.write_task(TASK_ID, batch)
        # 服务端真实收到了两点请求（拒绝发生在确认计数核对阶段）。
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

        await publish(r5_stack, make_sample("rotor_speed", 8.0, sample_id="r5g-slow"), "realtime")

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
    gw_rpc.add_GatewayDataServiceServicer_to_server(gw2, server2)
    bound = server2.add_insecure_port(f"127.0.0.1:{port}")
    assert bound == port
    await server2.start()
    try:
        await wait_state(sink, "grpc_sink", "running", timeout=60.0)
        await publish(
            r5_stack, make_sample("rotor_speed", 9.0, sample_id="r5g-recovered"), "realtime"
        )

        async def _recovered() -> tuple[str, str, Any] | None:
            for device, point, value in gw2.received:
                if (device, point) == ("gw-1", "speed") and value == pytest.approx(9.0):
                    return device, point, value
            return None

        await wait_until(_recovered, timeout=30.0, interval=0.25, description="post-restart write")
        status = await mgmt(sink, "GetSinkStatus", "grpc_sink")
        assert status["failure"] is None
    finally:
        await server2.stop(grace=1.0)
