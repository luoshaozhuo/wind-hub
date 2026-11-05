"""§14.4 表 14-2 并发/期限系统测试（UR-04，UC-02-S02）。

场景：有效期短于连接期限的命令在真实不可路由链路上不得被裁定成功；
gRPC 客户端断连不得撤销服务端已接受的命令（接受是服务端状态）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import commander_pb2 as pb
from contracts import commander_pb2_grpc, common_pb2

pytestmark = [
    pytest.mark.system,
    pytest.mark.reliability,
    pytest.mark.real_service,
]

_BLACKHOLE_HOST = "192.0.2.1"
_TERMINAL = {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"}
# 逻辑期限到期时底层连接无可信结束证据：只能是保守裁决，绝不能成功。
_CONSERVATIVE_OUTCOMES = {"UNKNOWN", "NOT_SENT"}


def _tz01_to_blackhole(service_dir: Path) -> None:
    devices = service_dir / "config" / "instances" / "devices.yaml"
    text = devices.read_text(encoding="utf-8")
    prefix, rest = text.split("  TZ01:", 1)
    block, suffix = rest.split("  TZ02:", 1)
    block = block.replace("host: 127.0.0.1", f"host: {_BLACKHOLE_HOST}", 1)
    devices.write_text(prefix + "  TZ01:" + block + "  TZ02:" + suffix, encoding="utf-8")


@pytest.mark.timeout
@pytest.mark.exception
@pytest.mark.parametrize("grpc_stack_prepared", [_tz01_to_blackhole], indirect=True)
async def test_expiration_and_unknown(sys_pki, grpc_stack_prepared) -> None:
    """UC-02-S02：短有效期 + 悬挂连接 → 保守裁决；客户端断连不撤销已接受命令。"""
    stack = grpc_stack_prepared

    # --- 第一部分：有效期早于连接建立，命令不得成功，I/O 不得虚报 settled ---
    channel = stack.channel(sys_pki, client="ops")
    commander = commander_pb2_grpc.CommanderServiceStub(channel)
    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    revision = next(d.revision for d in devices.devices if d.device_id == "TZ01")
    points = await commander.ListPoints(pb.ListPointsRequest(device_id="TZ01"), timeout=5)
    point = next(p for p in points.points if p.writable)
    expires = Timestamp()
    expires.FromDatetime(datetime.now(UTC) + timedelta(milliseconds=800))
    submitted = await commander.SubmitCommand(
        pb.SubmitCommandRequest(
            device_id="TZ01",
            point_id=point.point_id,
            value=common_pb2.ScalarValue(sint64_value=1),
            expected_revision=revision,
            expires_at=expires,
        ),
        timeout=5,
    )
    assert submitted.accepted is True
    command_id = submitted.command_id
    # 客户端在命令仍在途时直接断开：接受是服务端状态，不随 RPC/通道撤销。
    await channel.close()

    # 用全新通道继续查询同一命令。
    _common, commander2 = stack.stubs(sys_pki, client="ops")
    deadline = datetime.now(UTC) + timedelta(seconds=8)
    result = None
    while datetime.now(UTC) < deadline:
        result = await commander2.GetCommandResult(
            pb.GetCommandResultRequest(command_id=command_id), timeout=5
        )
        if result.state in _TERMINAL:
            break
        await asyncio.sleep(0.05)
    assert result is not None and result.state in _TERMINAL
    assert result.state != "SUCCEEDED"
    assert result.outcome in _CONSERVATIVE_OUTCOMES
    assert result.io_settled is False
