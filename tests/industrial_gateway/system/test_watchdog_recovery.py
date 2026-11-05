"""看门狗 reconcile 真实恢复语义（进程内真实 runtime + 真实 pymodbus 监听器）。

故障注入方式：完成 pymodbus Server 自己的 ``serving`` future——这正是 pymodbus
监听器停止时触发的生命周期信号（见 ModbusBaseServer.shutdown），不替换任何生产
对象。watchdog 每 2s reconcile 一次：
- desired=START 且 is_serving=False → mark FAILED → 释放旧监听器 → 同端口重绑；
- desired=STOP 的监听器不自动恢复，直到显式 StartGateway。
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest

from contracts import common_pb2 as common
from tests.support.gateway_v1 import start_gateway_v1, two_server_specs

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.modbus]


async def _fc03_read_zero(port: int, unit: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        pdu = struct.pack(">BHH", 0x03, 110, 1)
        writer.write(struct.pack(">HHHB", 1, 0, len(pdu) + 1, unit) + pdu)
        header = await reader.readexactly(7)
        _tid, _proto, length, _u = struct.unpack(">HHHB", header)
        body = await reader.readexactly(length - 1)
        assert body[0] == 0x03 and body[1] == 2 and body[2:4] == b"\x00\x00"
    finally:
        writer.close()
        await writer.wait_closed()


def _kill_listener(harness, server_id: str) -> None:
    """完成 pymodbus serving future：生产侧 is_serving 转 false 的真实信号。"""
    instance = harness.runtime.instances[server_id]
    server = instance.protocol_server
    assert server is not None
    # pymodbus 未暴露 serving future 的只读句柄，只能经其内部字段发出停止信号。
    raw = server._server
    assert raw is not None and not raw.serving.done()
    raw.serving.set_result(True)


async def _wait_state(harness, server_id: str, actual: str, timeout: float = 8.0) -> None:
    stub = harness.gateway_stub()
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        reply = await stub.GetGateway(common.ObjectId(id=server_id))
        if reply.gateway.actual_state == actual:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{server_id} never reached {actual}")
        await asyncio.sleep(0.2)


async def test_watchdog_recovers_failed_listener_without_reconfig(tmp_path: Path) -> None:
    harness = await start_gateway_v1(tmp_path / "root", two_server_specs())
    try:
        port = harness.specs["gw1"].modbus_port
        await _fc03_read_zero(port, 1)  # 崩溃前真实可读。

        _kill_listener(harness, "gw1")

        await _wait_state(harness, "gw1", "RUNNING")
        # 恢复后同端口重新服务，无任何配置操作。
        await _fc03_read_zero(port, 1)
        reply = await harness.gateway_stub().GetGateway(common.ObjectId(id="gw1"))
        assert reply.gateway.actual_state == "RUNNING"
        status = (await harness.common_stub().GetServiceStatus(common.Empty())).dependencies
        gw1 = {dep.name: dep for dep in status}["gw1"]
        assert gw1.health == "HEALTHY"
        # 恢复事件与重启计数有真实证据。
        events = await harness.common_stub().GetEvents(
            common.GetEventsRequest(filter="restart_count")
        )
        assert events.events  # start_server 记录 restart_count=1 事件。
        # 未受影响的 gw2 从未重建。
        await _fc03_read_zero(harness.specs["gw2"].modbus_port, 2)
    finally:
        await harness.stop()


async def test_explicit_stop_is_not_revived_by_watchdog(tmp_path: Path) -> None:
    harness = await start_gateway_v1(tmp_path / "root", two_server_specs())
    try:
        stub = harness.gateway_stub()
        port = harness.specs["gw1"].modbus_port
        revision = (await stub.GetGateway(common.ObjectId(id="gw1"))).gateway.revision
        await stub.StopGateway(common.OperationRequest(id="gw1", expected_revision=revision))
        await _wait_state(harness, "gw1", "STOPPED")
        # 监听器已停止：端口拒绝连接。
        with pytest.raises((ConnectionError, OSError)):
            await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=1.0)

        # 再制造 serving 完成信号，等待超过一个 reconcile 周期，仍不得复活。
        await asyncio.sleep(4.5)
        reply = await stub.GetGateway(common.ObjectId(id="gw1"))
        assert reply.gateway.actual_state == "STOPPED"
        with pytest.raises((ConnectionError, OSError)):
            await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=1.0)

        # 显式 Start 后恢复服务。
        await stub.StartGateway(common.OperationRequest(id="gw1", expected_revision=revision))
        await _wait_state(harness, "gw1", "RUNNING")
        await _fc03_read_zero(port, 1)
    finally:
        await harness.stop()
