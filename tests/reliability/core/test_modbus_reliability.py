"""Core ModbusDriver 可靠性矩阵（真实 TCP，无 mock 网络）。

覆盖 §27 故障模型：连接拒绝、读中断链、服务端重启后重连、
IO 中取消、重复断开与资源释放。复用集成层的 pymodbus 进程内
Server 与静默 Server 故障注入。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from core.application.errors import ProtocolError
from core.infrastructure.protocol.modbus.driver import ModbusDriver
from tests.integration.protocols.modbus.test_core_modbus_tcp import (
    _driver,
    _SilentServer,
    _start_server,
)
from tests.support.process import free_port
from tests.support.wait import wait_until

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


async def test_read_timeout_on_silent_server_recovers_after_reconnect() -> None:
    """静默对端触发真实 IO 超时：请求失败、不可复用 transport 被释放，
    重连到恢复的服务端后可重新读回数据（Core 不做应用层重试，retries=0）。"""
    silent = _SilentServer()
    port = await silent.start()
    driver = _driver(port, timeout=0.5)
    await driver.connect()
    try:
        with pytest.raises(ProtocolError):
            await driver.read_one("hr.int")
        assert silent.frames >= 1
        assert driver.is_open() is False
    finally:
        await driver.close()
        await silent.stop()

    server = await _start_server(port)
    try:
        await driver.connect()
        sample = await driver.read_one("hr.int")
        assert sample.value == 4321
    finally:
        await driver.close()
        await server.shutdown()


class _DroppingServer:
    """接受 TCP 连接并读取 MBAP 帧但永不响应；可主动切断全部连接。"""

    def __init__(self) -> None:
        self.frames = 0
        self._server: asyncio.AbstractServer | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            while True:
                header = await reader.readexactly(7)  # MBAP 头
                length = int.from_bytes(header[4:6], "big")
                if length > 1:
                    await reader.readexactly(length - 1)
                self.frames += 1
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            pass

    async def drop_connections(self) -> None:
        for writer in self._writers:
            writer.close()
        for writer in self._writers:
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def stop(self) -> None:
        await self.drop_connections()
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


async def test_concurrent_reads_during_disconnect_all_fail_without_hang() -> None:
    """并发读期间服务端断线：全部在途读显式失败，无挂起、无残留 task。"""
    dropping = _DroppingServer()
    port = await dropping.start()
    driver = _driver(port, timeout=2.0)
    await driver.connect()
    try:
        tasks = [asyncio.create_task(driver.read_one("hr.int")) for _ in range(5)]
        await wait_until(lambda: dropping.frames >= 1, timeout=5.0)
        # 多请求在途（pymodbus 串行化，后续请求排队等待）时切断全部连接。
        await dropping.drop_connections()

        outcomes = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=30.0,
        )
        for outcome in outcomes:
            assert isinstance(outcome, ProtocolError), outcome
        assert driver.is_open() is False
        # 断线后不得静默复用旧 transport。
        with pytest.raises(ProtocolError):
            await driver.read_one("hr.int")
    finally:
        await driver.close()
        await dropping.stop()
