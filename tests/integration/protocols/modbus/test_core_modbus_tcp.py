"""新 Core ModbusDriver × 真实 pymodbus TCP server 集成测试。

被测组件是 ``src/core/infrastructure/protocol/modbus/driver.py`` 的
ModbusDriver（含 RecoveringProtocol 恢复路径）；对端是 pymodbus 进程内
ModbusTcpServer（真实 TCP，127.0.0.1 动态端口）。网络故障类场景使用
受控的静默 TCP server（接受连接但不应答）做确定性故障注入。
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator

import pytest

from core.application.errors import ProtocolError
from core.application.protocol_contract import ProtocolWrite
from core.application.recovery import RecoveringProtocol, RecoverySettings
from core.domain import (
    UNIT_CATALOG,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    Protocol,
    UnitCode,
)
from core.infrastructure.protocol.modbus.driver import ModbusDriver
from tests.support.process import free_port

pytestmark = pytest.mark.real_service


# ---------------------------------------------------------------------------
# 测试数据布局（wire 地址）
# ---------------------------------------------------------------------------

_COIL_RO = 0
_COIL_RW = 5
_DI_RO = 0
_HR_INT = 10
_HR_FLOAT = 100  # float32，占 100-101
_HR_FLOAT2 = 102  # float32，与 _HR_FLOAT 连续
_HR_FAR = 120
_HR_WINT = 300
_HR_WFLOAT = 302  # float32，占 302-303
_IR_FLOAT = 200  # float32，占 200-201
_HR_OOB = 9000  # 超出数据块，用于 exception response


def _f32(value: float) -> list[int]:
    return list(struct.unpack(">HH", struct.pack(">f", value)))


def _point(
    point_id: str,
    register_type: str,
    address: int,
    data_type: str,
    access: PointAccess = PointAccess.READ_WRITE,
) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=access,
        ext={
            "register_type": register_type,
            "address": address,
            "data_type": data_type,
        },
    )


def _table() -> PointTable:
    points = [
        _point("coil.ro", "coil", _COIL_RO, "bool"),
        _point("coil.rw", "coil", _COIL_RW, "bool"),
        _point("di.ro", "discrete_input", _DI_RO, "bool", PointAccess.READ),
        _point("hr.int", "holding", _HR_INT, "uint16"),
        _point("hr.float", "holding", _HR_FLOAT, "float32"),
        _point("hr.float2", "holding", _HR_FLOAT2, "float32"),
        _point("hr.far", "holding", _HR_FAR, "uint16"),
        _point("hr.wint", "holding", _HR_WINT, "int16"),
        _point("hr.wfloat", "holding", _HR_WFLOAT, "float32"),
        _point("ir.float", "input", _IR_FLOAT, "float32", PointAccess.READ),
        _point("hr.oob", "holding", _HR_OOB, "uint16"),
    ]
    return PointTable("pt", Protocol("modbus"), {p.point_id: p for p in points})


def _datastore(hr_overrides: dict[int, int] | None = None):
    from pymodbus.datastore import (
        ModbusDeviceContext,
        ModbusSequentialDataBlock,
        ModbusServerContext,
    )

    # addr=1 → values[i] 落在 wire 地址 i（与既有 legacy 集成测试同一约定）。
    coils = [False] * 32
    coils[_COIL_RO] = True
    discretes = [False] * 32
    discretes[_DI_RO] = True
    hr = [0] * 512
    hr[_HR_INT] = 4321
    hr[_HR_FLOAT : _HR_FLOAT + 2] = _f32(1500.5)
    hr[_HR_FLOAT2 : _HR_FLOAT2 + 2] = _f32(2.5)
    hr[_HR_FAR] = 777
    for address, value in (hr_overrides or {}).items():
        hr[address] = value
    ir = [0] * 256
    ir[_IR_FLOAT : _IR_FLOAT + 2] = _f32(42.5)

    store = ModbusDeviceContext(
        co=ModbusSequentialDataBlock(1, coils),
        di=ModbusSequentialDataBlock(1, discretes),
        hr=ModbusSequentialDataBlock(1, hr),
        ir=ModbusSequentialDataBlock(1, ir),
    )
    return ModbusServerContext(devices={1: store}, single=False)


async def _start_server(port: int, hr_overrides: dict[int, int] | None = None):
    from pymodbus.server import ModbusTcpServer

    server = ModbusTcpServer(
        _datastore(hr_overrides), address=("127.0.0.1", port)
    )
    await server.serve_forever(background=True)
    return server


def _driver(port: int, *, timeout: float = 2.0) -> ModbusDriver:
    return ModbusDriver(
        ConnectionEndpoint("127.0.0.1", port),
        _table(),
        {"word_order": "big_endian", "timeout": timeout},
    )


@pytest.fixture
async def modbus_env() -> AsyncIterator[tuple[ModbusDriver, object, int]]:
    port = free_port()
    server = await _start_server(port)
    driver = _driver(port)
    await driver.connect()
    try:
        yield driver, server, port
    finally:
        await driver.close()
        await server.shutdown()


# ---------------------------------------------------------------------------
# 真实 Server 读写
# ---------------------------------------------------------------------------


class TestRealServerIO:
    async def test_connect_reports_healthy(
        self, modbus_env: tuple[ModbusDriver, object, int]
    ) -> None:
        driver, _, _ = modbus_env
        assert driver.health().healthy is True

    async def test_read_one_all_register_types(
        self, modbus_env: tuple[ModbusDriver, object, int]
    ) -> None:
        driver, _, _ = modbus_env
        assert (await driver.read_one("coil.ro")).value is True
        assert (await driver.read_one("di.ro")).value is True
        assert (await driver.read_one("hr.int")).value == 4321
        assert (await driver.read_one("ir.float")).value == pytest.approx(42.5)

    async def test_read_one_multi_register_word_order(
        self, modbus_env: tuple[ModbusDriver, object, int]
    ) -> None:
        driver, _, _ = modbus_env
        sample = await driver.read_one("hr.float")
        assert sample.value == pytest.approx(1500.5)
        assert sample.quality == "good"

    async def test_read_many_merge_split_and_order(
        self, modbus_env: tuple[ModbusDriver, object, int]
    ) -> None:
        driver, _, _ = modbus_env
        samples = await driver.read_many(("hr.far", "hr.float2", "hr.float", "coil.ro"))
        assert [s.point_id for s in samples] == ["hr.far", "hr.float2", "hr.float", "coil.ro"]
        by_id = {s.point_id: s for s in samples}
        assert by_id["hr.float"].value == pytest.approx(1500.5)
        assert by_id["hr.float2"].value == pytest.approx(2.5)
        assert by_id["hr.far"].value == 777
        assert by_id["coil.ro"].value is True
        assert all(s.quality == "good" for s in samples)

    async def test_write_one_coil_roundtrip(
        self, modbus_env: tuple[ModbusDriver, object, int]
    ) -> None:
        driver, _, _ = modbus_env
        result = await driver.write_one(ProtocolWrite("coil.rw", True))
        assert result.success
        assert (await driver.read_one("coil.rw")).value is True

    async def test_write_one_single_register_roundtrip(
        self, modbus_env: tuple[ModbusDriver, object, int]
    ) -> None:
        driver, _, _ = modbus_env
        result = await driver.write_one(ProtocolWrite("hr.wint", -123))
        assert result.success
        assert (await driver.read_one("hr.wint")).value == -123

    async def test_write_one_multi_register_roundtrip(
        self, modbus_env: tuple[ModbusDriver, object, int]
    ) -> None:
        driver, _, _ = modbus_env
        result = await driver.write_one(ProtocolWrite("hr.wfloat", 66.25))
        assert result.success
        assert (await driver.read_one("hr.wfloat")).value == pytest.approx(66.25)

    async def test_write_many_rejected_without_side_effects(
        self, modbus_env: tuple[ModbusDriver, object, int]
    ) -> None:
        driver, _, _ = modbus_env
        with pytest.raises(NotImplementedError):
            await driver.write_many(())
        with pytest.raises(NotImplementedError):
            await driver.write_many((ProtocolWrite("hr.wint", 55),))
        assert (await driver.read_one("hr.wint")).value == 0  # 未发生任何写入
        assert driver.health().healthy is True  # 不支持 ≠ 断线

    async def test_exception_response_keeps_connection(
        self, modbus_env: tuple[ModbusDriver, object, int]
    ) -> None:
        driver, _, _ = modbus_env
        with pytest.raises(ProtocolError, match="exception response"):
            await driver.read_one("hr.oob")
        assert driver.health().healthy is True
        # 连接仍可用
        assert (await driver.read_one("hr.int")).value == 4321

    async def test_close_releases_connection(
        self, modbus_env: tuple[ModbusDriver, object, int]
    ) -> None:
        driver, _, _ = modbus_env
        await driver.close()
        assert driver.health().healthy is False
        await driver.close()  # 幂等


# ---------------------------------------------------------------------------
# Server 停止/恢复 与 RecoveringProtocol 重连
# ---------------------------------------------------------------------------


class TestRecoveryOverRealTcp:
    async def test_reconnect_after_server_restart(self) -> None:
        port = free_port()
        server = await _start_server(port)
        driver = _driver(port)
        port_if = RecoveringProtocol(driver, RecoverySettings(reconnect_attempts=2))
        await port_if.connect()
        try:
            assert (await port_if.read_one("hr.int")).value == 4321

            await server.shutdown()
            await asyncio.sleep(0.2)  # 等待 client 感知 connection_lost

            # Server 下线期间读取失败，且连接状态已失效。
            with pytest.raises(ProtocolError):
                await port_if.read_one("hr.int")
            assert driver.health().healthy is False

            # Server 以新数据恢复：下一次读取重连成功并取得新值。
            server = await _start_server(port, hr_overrides={_HR_INT: 9999})
            assert (await port_if.read_one("hr.int")).value == 9999
        finally:
            await port_if.close()
            await server.shutdown()


# ---------------------------------------------------------------------------
# 静默 TCP server：确定性故障注入（接受连接、计数帧、永不应答）
# ---------------------------------------------------------------------------


class _SilentServer:
    """接受 TCP 连接并完整读取 MBAP 帧但永不响应；用于超时/取消场景。"""

    def __init__(self) -> None:
        self.frames = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                header = await reader.readexactly(7)  # MBAP 头
                length = int.from_bytes(header[4:6], "big")
                if length > 1:
                    await reader.readexactly(length - 1)
                self.frames += 1
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


class TestTimeoutAndCancellation:
    async def test_read_timeout_invalidates_connection(self) -> None:
        silent = _SilentServer()
        port = await silent.start()
        driver = _driver(port)
        port_if = RecoveringProtocol(
            driver, RecoverySettings(reconnect_attempts=1, read_timeout=0.2)
        )
        await port_if.connect()
        try:
            with pytest.raises(ProtocolError, match="timed out"):
                await port_if.read_one("hr.int")
            # 超时后不允许残留健康状态。
            assert driver.health().healthy is False
        finally:
            await port_if.close()
            await silent.stop()

    async def test_write_timeout_never_resends(self) -> None:
        silent = _SilentServer()
        port = await silent.start()
        driver = _driver(port)
        port_if = RecoveringProtocol(driver, RecoverySettings(write_timeout=0.2))
        await port_if.connect()
        try:
            with pytest.raises(ProtocolError, match="timed out"):
                await port_if.write_one(ProtocolWrite("hr.wint", 55))
            await asyncio.sleep(0.4)  # 留出窗口观察是否存在重发
            assert silent.frames == 1  # 写请求只发送过一次
            assert driver.health().healthy is False
        finally:
            await port_if.close()
            await silent.stop()

    async def test_upper_cancellation_propagates_without_deadlock(self) -> None:
        silent = _SilentServer()
        port = await silent.start()
        driver = _driver(port)
        port_if = RecoveringProtocol(driver, RecoverySettings(read_timeout=5.0))
        await port_if.connect()
        try:
            task = asyncio.create_task(port_if.read_one("hr.int"))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # 取消后连接按未知状态失效，锁不残留。
            assert driver.health().healthy is False
            assert not driver._lock.locked()
        finally:
            await port_if.close()  # 不死锁即通过
            await silent.stop()
