"""实时读写 functional 测试——真实 Modbus TCP fixture server。

读路径断言协议原始值；写路径在写入后从 fixture server 用独立客户端
回读寄存器，确认值真正到达「设备侧」。错误语义覆盖未知设备/点、
协议故障（对端不可达）、读超时（对端不应答）与强制重连恢复。
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator

import pytest

from tests.component.collector.conftest import FunctionalContext
from tests.support.process import free_port
from wind_hub_core.model.command import Command
from wind_hub_core.model.errors import CommandError, ProtocolError

pytestmark = pytest.mark.modbus


def _decode_float32(registers: list[int]) -> float:
    """按 fixture 的 big-endian 双寄存器布局解码 float32。"""
    hi, lo = registers
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]


class TestReadPoint:
    async def test_read_single_point_returns_protocol_value(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        value = await modbus_runtime.rt.query.read_point("modbus-1", "rotor.speed")
        assert value.device_id == "modbus-1"
        assert value.point_id == "rotor.speed"
        assert value.value == pytest.approx(1200.5)

    async def test_read_int16_point(self, modbus_runtime: FunctionalContext) -> None:
        value = await modbus_runtime.rt.query.read_point("modbus-1", "temp.int")
        assert value.value == 25

    async def test_read_unknown_device_raises_command_error(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        with pytest.raises(CommandError, match="unknown device"):
            await modbus_runtime.rt.query.read_point("ghost", "rotor.speed")

    async def test_read_unknown_point_raises_command_error(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        with pytest.raises(CommandError, match="unknown point"):
            await modbus_runtime.rt.query.read_point("modbus-1", "ghost.point")


class TestWritePoint:
    async def test_write_reaches_device_registers(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        result = await modbus_runtime.rt.command.send(
            Command(
                command_id="write-1",
                device_id="modbus-1",
                point_id="setpoint.power",
                value=1.5,
            )
        )
        assert result.success, result.error

        # 独立客户端从 server 侧回读——确认写入到达协议对端。
        server = modbus_runtime.server
        assert server is not None
        registers = await server.read_holding(unit_id=1, address=200, count=2)
        assert _decode_float32(registers) == pytest.approx(1.5)

    async def test_write_unknown_device_returns_failed_result(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        result = await modbus_runtime.rt.command.send(
            Command(
                command_id="write-unknown-device",
                device_id="ghost",
                point_id="setpoint.power",
                value=1.0,
            )
        )
        assert not result.success
        assert result.error is not None
        assert "ghost" in result.error


class TestProtocolFailure:
    async def test_read_from_unreachable_device_raises_protocol_error(
        self, runtime_factory
    ) -> None:
        # 设备指向无监听端口：强制重连失败 → ProtocolError。
        async with runtime_factory(with_server=False) as ctx:
            with pytest.raises(ProtocolError, match="not connected"):
                await ctx.rt.query.read_point("modbus-1", "rotor.speed")

    async def test_write_to_unreachable_device_returns_failed_result(
        self, runtime_factory
    ) -> None:
        async with runtime_factory(with_server=False) as ctx:
            result = await ctx.rt.command.send(
                Command(
                    command_id="write-unreachable",
                    device_id="modbus-1",
                    point_id="setpoint.power",
                    value=1.0,
                )
            )
            assert not result.success
            assert result.error is not None
            assert "not connected" in result.error


class _SilentServer:
    """接受 TCP 连接但永不应答——用于触发协议读超时。"""

    def __init__(self) -> None:
        self.port = free_port()
        self._server: asyncio.AbstractServer | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._closed = False

    async def start(self) -> None:
        async def _hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await reader.read(1024)
                await asyncio.sleep(3600)
            except (asyncio.CancelledError, ConnectionError):
                pass
            finally:
                writer.close()

        def _on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            # stop 后仍可到达的迟接入（TCP 握手先于 listener 关闭完成）：
            # 直接关闭，不再创建 handler 任务。
            if self._closed:
                writer.close()
                return
            # start_server 的协程回调在任务泄露检查中不可见——显式建任务
            # 并在 stop 时统一取消，避免测试结束遗留 pending handler。
            task = asyncio.ensure_future(_hold(reader, writer))
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)

        self._server = await asyncio.start_server(
            _on_connect, "127.0.0.1", self.port
        )

    async def stop(self) -> None:
        self._closed = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in self._handlers:
            task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)


@pytest.fixture
async def silent_server() -> AsyncIterator[_SilentServer]:
    server = _SilentServer()
    await server.start()
    yield server
    await server.stop()


class TestReadTimeout:
    async def test_read_against_silent_peer_times_out(
        self, runtime_factory, silent_server: _SilentServer
    ) -> None:
        # TCP 可连接但协议层永不应答：读必须按超时失败而非悬挂。
        async with runtime_factory(
            port=silent_server.port, with_server=False
        ) as ctx:
            with pytest.raises(ProtocolError):
                await asyncio.wait_for(
                    ctx.rt.query.read_point("modbus-1", "rotor.speed"),
                    timeout=10.0,
                )


class TestForceReconnect:
    async def test_read_recovers_after_server_restart(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        server = modbus_runtime.server
        assert server is not None

        # 对端宕机：读失败（ProtocolError 或其上层的连接失败）。
        await server.stop()
        with pytest.raises(ProtocolError):
            await modbus_runtime.rt.query.read_point("modbus-1", "rotor.speed")

        # 对端恢复：read_point 内部强制重连，下一轮读成功。
        await server.start()
        value = await modbus_runtime.rt.query.read_point("modbus-1", "rotor.speed")
        assert value.value == pytest.approx(1200.5)
