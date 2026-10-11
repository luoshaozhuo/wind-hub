"""Core ModbusDriver 可靠性矩阵（真实 TCP，无 mock 网络）。

覆盖 §27 故障模型：连接拒绝、读中断链、服务端重启后重连、
IO 中取消、重复断开与资源释放。复用集成层的 pymodbus 进程内
Server 与静默 Server 故障注入。
"""

from __future__ import annotations

import asyncio

import pytest

from core.application.errors import ProtocolError
from core.infrastructure.protocol.modbus.driver import ModbusDriver
from tests.integration.protocols.modbus.test_core_modbus_tcp import (
    _driver,
    _SilentServer,
    _start_server,
)
from tests.support.process import free_port

pytestmark = pytest.mark.real_service


async def test_connect_refused_fails_explicitly_and_close_is_idempotent() -> None:
    driver = _driver(free_port())  # 无监听者 → ECONNREFUSED
    # 建连失败属于 ProtocolError（建连/握手类）；链路级断链才是
    # ProtocolConnectionError。显式失败，不得静默降级。
    with pytest.raises(ProtocolError, match="rejected|failed|refus"):
        await driver.connect()
    assert driver.is_open() is False
    await driver.close()
    await driver.close()
    assert driver.is_open() is False


async def test_read_after_server_stop_marks_driver_closed() -> None:
    port = free_port()
    server = await _start_server(port)
    driver = _driver(port)
    await driver.connect()
    assert driver.is_open()
    await server.shutdown()
    try:
        with pytest.raises(ProtocolError):
            await driver.read_one("hr.int")
        assert driver.is_open() is False
    finally:
        await driver.close()


async def test_server_restart_allows_fresh_reconnect() -> None:
    port = free_port()
    server = await _start_server(port)
    driver = _driver(port)
    await driver.connect()
    await server.shutdown()
    with pytest.raises(ProtocolError):
        await driver.read_one("hr.int")

    server = await _start_server(port)
    try:
        await driver.connect()
        sample = await driver.read_one("hr.int")
        assert sample.value == 4321
    finally:
        await driver.close()
        await server.shutdown()


async def test_cancel_during_inflight_read_closes_transport() -> None:
    """IO 中取消：结果归属不可判定，驱动必须显式断链而不是静默复用。"""
    silent = _SilentServer()
    port = await silent.start()
    driver: ModbusDriver = _driver(port, timeout=30.0)
    await driver.connect()
    try:
        task = asyncio.create_task(driver.read_one("hr.int"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert driver.is_open() is False
        with pytest.raises(ProtocolError):
            await driver.read_one("hr.int")
    finally:
        await driver.close()
        await silent.stop()


async def test_double_disconnect_while_connected_is_safe() -> None:
    port = free_port()
    server = await _start_server(port)
    driver = _driver(port)
    await driver.connect()
    try:
        await driver.close()
        assert driver.is_open() is False
        await driver.close()
        assert driver.is_open() is False
    finally:
        await server.shutdown()


async def test_close_releases_transport_and_leaves_no_driver_tasks() -> None:
    port = free_port()
    server = await _start_server(port)
    driver = _driver(port)
    await driver.connect()
    sample = await driver.read_one("hr.int")
    assert sample.value == 4321
    await driver.close()
    try:
        assert driver.is_open() is False
        # 关闭后健康探测不得重连，仅报告不健康。
        health = await driver.health()
        assert health.healthy is False
        assert driver.is_open() is False
    finally:
        await server.shutdown()
