"""真实 Modbus TCP 链路集成：pymodbus server + ModbusDriver 经 QueuedExecutor。

不模拟协议栈；executor 做真实 T1/T2/T3 落盘，driver 发真实 FC06/FC16，
结果以保持寄存器回读为准。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from commander.application.queued_executor import QueuedExecutor
from commander.domain.command import (
    CommandOutcome,
    CommandSpec,
    CommandState,
)
from core.application.protocol_contract import ProtocolWrite
from core.domain import ConnectionEndpoint, PointAccess
from core.infrastructure.protocol.modbus import ModbusDriver
from tests.core.protocol.conftest import make_point, make_table
from tests.support.process import free_port

pytestmark = [pytest.mark.integration, pytest.mark.modbus, pytest.mark.real_service]


def _driver_factory(port: int) -> dict[str, Any]:
    table = make_table(
        "modbus",
        make_point("command", register_type="holding", address=200, data_type="float32"),
        make_point("temperature", register_type="holding", address=202, data_type="int16"),
    )

    def factory(_device_id: str) -> ModbusDriver:
        return ModbusDriver(
            ConnectionEndpoint("127.0.0.1", port),
            table,
            {"word_order": "big_endian", "timeout": 2.0},
        )

    return {"factory": factory, "table": table}


async def test_real_modbus_write_confirmed_and_persisted(modbus_server, control) -> None:
    bundle = _driver_factory(modbus_server.port)
    executor = QueuedExecutor(
        control,
        bundle["factory"],
        lambda _spec: True,
        lambda spec: ProtocolWrite(point=spec.point_id, value=spec.value),
        connect_timeout=3.0,
        write_timeout=3.0,
    )
    accepted = await executor.accept(
        [CommandSpec("WT01", "command", 42.5, "ref-v1")], caller_id="ops"
    )
    try:
        waited = await asyncio.wait_for(executor.wait(accepted[0].command_id), timeout=5.0)
        assert waited is not None
        result = waited
    finally:
        blocked = await executor.close()
        assert blocked == frozenset()
    assert result.state is CommandState.SUCCEEDED
    assert result.outcome is CommandOutcome.CONFIRMED_SUCCESS
    # 现场回读证实物理写入，且全事件链留在真实 SQLite 中。
    driver: ModbusDriver = bundle["factory"]("verify")
    await driver.connect()
    try:
        sample = await driver.read_one("command")
        assert sample.value == pytest.approx(42.5)
    finally:
        await driver.close()
    events = await control.get_command(accepted[0].command_id)
    assert events.state is CommandState.SUCCEEDED
    assert events.completed_at is not None


async def test_real_modbus_batch_writes_each_point_separately_without_write_many(
    modbus_server, control
) -> None:
    # ModbusDriver 不声明 WRITE_MANY：一批两点必须顺序走两次 FC 写并各自确认。
    bundle = _driver_factory(modbus_server.port)
    executor = QueuedExecutor(
        control,
        bundle["factory"],
        lambda _spec: True,
        lambda spec: ProtocolWrite(point=spec.point_id, value=spec.value),
        max_batch_points=10,
        connect_timeout=3.0,
        write_timeout=3.0,
    )
    accepted = await executor.accept(
        [
            CommandSpec("WT01", "command", 7.0, "ref-v1"),
            CommandSpec("WT01", "temperature", 31, "ref-v1"),
        ],
        caller_id="ops",
    )
    try:
        results = await asyncio.gather(
            *(asyncio.wait_for(executor.wait(c.command_id), timeout=5.0) for c in accepted)
        )
    finally:
        await executor.close()
    assert all(r is not None for r in results)
    assert [r.state for r in results if r is not None] == [
        CommandState.SUCCEEDED,
        CommandState.SUCCEEDED,
    ]
    driver: ModbusDriver = bundle["factory"]("verify")
    await driver.connect()
    try:
        assert (await driver.read_one("command")).value == pytest.approx(7.0)
        assert (await driver.read_one("temperature")).value == 31
    finally:
        await driver.close()


async def test_unreachable_modbus_connect_is_confirmed_failure(control) -> None:
    port = free_port()
    bundle = _driver_factory(port)
    executor = QueuedExecutor(
        control,
        bundle["factory"],
        lambda _spec: True,
        lambda spec: ProtocolWrite(point=spec.point_id, value=spec.value),
        connect_timeout=2.0,
        write_timeout=2.0,
    )
    accepted = await executor.accept(
        [CommandSpec("WT01", "command", 1.0, "ref-v1")], caller_id="ops"
    )
    try:
        waited = await asyncio.wait_for(executor.wait(accepted[0].command_id), timeout=5.0)
        assert waited is not None
        result = waited
    finally:
        await executor.close()
    # 连接阶段失败可确证未发送：FAILED/NOT 现场 UNKNOWN。
    assert result.state is CommandState.FAILED
    assert result.error_code == "CONNECT_FAILED"


async def test_point_without_write_access_rejected_before_wire(modbus_server, control) -> None:
    table = make_table(
        "modbus",
        make_point(
            "speed",
            access=PointAccess.READ,
            register_type="holding",
            address=100,
            data_type="float32",
        ),
    )
    driver_factory = lambda _device_id: ModbusDriver(  # noqa: E731
        ConnectionEndpoint("127.0.0.1", modbus_server.port),
        table,
        {"word_order": "big_endian", "timeout": 2.0},
    )
    executor = QueuedExecutor(
        control,
        driver_factory,
        lambda _spec: True,
        lambda spec: ProtocolWrite(point=spec.point_id, value=spec.value),
        connect_timeout=3.0,
        write_timeout=3.0,
    )
    accepted = await executor.accept([CommandSpec("WT01", "speed", 1.0, "ref-v1")], caller_id="ops")
    try:
        waited = await asyncio.wait_for(executor.wait(accepted[0].command_id), timeout=5.0)
        assert waited is not None
        result = waited
    finally:
        await executor.close()
    assert result.state is CommandState.FAILED
    assert result.outcome is CommandOutcome.CONFIRMED_FAILURE


def _three_point_table() -> Any:
    return make_table(
        "modbus",
        make_point("p0", register_type="holding", address=200, data_type="float32"),
        make_point("p1", register_type="holding", address=202, data_type="float32"),
        make_point("p2", register_type="holding", address=204, data_type="float32"),
    )


class _DriverWrapper:
    """委托全部会话接口给真实 ModbusDriver，仅截获 write_one。"""

    def __init__(self, driver: ModbusDriver) -> None:
        self._driver = driver
        self.physical: list[str] = []

    def capabilities(self) -> Any:
        return self._driver.capabilities()

    def is_open(self) -> bool:
        return self._driver.is_open()

    async def connect(self) -> None:
        await self._driver.connect()

    async def close(self) -> None:
        await self._driver.close()

    async def write_one(self, write: ProtocolWrite) -> Any:
        self.physical.append(write.point)
        return await self._driver.write_one(write)


class _DelayAckOnSecond(_DriverWrapper):
    """第二点 PDU 立即发出，但确认延迟到执行器写超时之后。"""

    def __init__(self, driver: ModbusDriver, delay: float) -> None:
        super().__init__(driver)
        self._delay = delay

    async def write_one(self, write: ProtocolWrite) -> Any:
        task = asyncio.create_task(super().write_one(write))
        if write.point == "p1":
            await asyncio.sleep(self._delay)
        return await task


class _CloseAfterFirst(_DriverWrapper):
    """p0 确认后真实关闭驱动会话（从站继续运行）：后续点提交前连接已消失。"""

    def __init__(self, driver: ModbusDriver) -> None:
        super().__init__(driver)

    async def write_one(self, write: ProtocolWrite) -> Any:
        result = await super().write_one(write)
        if len(self.physical) == 1:
            # 驱动公共生命周期：物理拆除 TCP（对端视角=连接消失），
            # is_open 同步翻 False；后续 write_one 在提交前即可证未发送。
            await self._driver.close()
        return result


async def test_real_modbus_rst_mid_batch_confirmed_not_sent_never_reaches_wire(
    modbus_server, control
) -> None:
    """p0 确认后连接真实消失：后续点可证未发送，只有 p0 到达 wire。

    真实 Modbus 驱动对"提交前会话已关闭"给 CONFIRMED_FAILURE（可证未发送），
    执行器对已确认失败不中止批次（矩阵第 5 行），p2 仍被调用但同样快速失败。
    物理边界以服务端 PDU 计数与寄存器回读为准：p0 落寄存器，p1/p2 保持原值。
    """
    table = _three_point_table()
    holder: dict[str, _CloseAfterFirst] = {}

    def factory(device_id: str) -> Any:
        wrapper = _CloseAfterFirst(
            ModbusDriver(
                ConnectionEndpoint("127.0.0.1", modbus_server.port),
                table,
                {"word_order": "big_endian", "timeout": 2.0},
            )
        )
        holder[device_id] = wrapper
        return wrapper

    executor = QueuedExecutor(
        control,
        factory,
        lambda _spec: True,
        lambda spec: ProtocolWrite(point=spec.point_id, value=spec.value),
        max_batch_points=10,
        connect_timeout=3.0,
        write_timeout=3.0,
    )
    probe = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", modbus_server.port),
        table,
        {"word_order": "big_endian", "timeout": 2.0},
    )
    await probe.connect()
    try:
        original_p1 = (await probe.read_one("p1")).value
        original_p2 = (await probe.read_one("p2")).value
    finally:
        await probe.close()

    accepted = await executor.accept(
        [
            CommandSpec("WT01", "p0", 11.5, "ref-v1"),
            CommandSpec("WT01", "p1", 22.5, "ref-v1"),
            CommandSpec("WT01", "p2", 33.5, "ref-v1"),
        ],
        caller_id="ops",
    )
    try:
        results = await asyncio.gather(
            *(asyncio.wait_for(executor.wait(c.command_id), timeout=8.0) for c in accepted),
            return_exceptions=True,
        )
        states = {
            c.spec.point_id: r.state
            for c, r in zip(accepted, results, strict=True)
            if r is not None and not isinstance(r, BaseException)
        }
        # p0 现场成功；RST 后 p1/p2 均在提交前被驱动证明未发送。
        assert states == {
            "p0": CommandState.SUCCEEDED,
            "p1": CommandState.FAILED,
            "p2": CommandState.FAILED,
        }
        # 执行器对每点恰好调用一次 write_one（失败点不重试）。
        assert holder["WT01"].physical == ["p0", "p1", "p2"]
    finally:
        await executor.close()

    # 物理证据：服务端只处理了 p0 的 FC06（从站全程在线，寄存器保留）。
    assert modbus_server.write_count == 1
    verifier = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", modbus_server.port),
        table,
        {"word_order": "big_endian", "timeout": 2.0},
    )
    await verifier.connect()
    try:
        assert (await verifier.read_one("p0")).value == pytest.approx(11.5)
        assert (await verifier.read_one("p1")).value == original_p1
        assert (await verifier.read_one("p2")).value == original_p2
    finally:
        await verifier.close()


async def test_real_modbus_write_timeout_but_applied_stays_unknown_no_overwrite(
    modbus_server, control
) -> None:
    """写超时但 PDU 已生效：寄存器已改、终态 UNKNOWN；底层收敛后不得改写终态。"""
    table = _three_point_table()

    def factory(_device_id: str) -> Any:
        return _DelayAckOnSecond(
            ModbusDriver(
                ConnectionEndpoint("127.0.0.1", modbus_server.port),
                table,
                {"word_order": "big_endian", "timeout": 3.0},
            ),
            delay=1.0,
        )

    executor = QueuedExecutor(
        control,
        factory,
        lambda _spec: True,
        lambda spec: ProtocolWrite(point=spec.point_id, value=spec.value),
        max_batch_points=10,
        connect_timeout=3.0,
        write_timeout=0.4,
    )
    accepted = await executor.accept(
        [
            CommandSpec("WT01", "p0", 111.25, "ref-v1"),
            CommandSpec("WT01", "p1", 222.5, "ref-v1"),
            CommandSpec("WT01", "p2", 333.75, "ref-v1"),
        ],
        caller_id="ops",
    )
    try:
        results = await asyncio.gather(
            *(asyncio.wait_for(executor.wait(c.command_id), timeout=8.0) for c in accepted),
            return_exceptions=True,
        )
        states_at_timeout = {
            c.spec.point_id: r.state
            for c, r in zip(accepted, results, strict=True)
            if r is not None and not isinstance(r, BaseException)
        }
        assert states_at_timeout == {
            "p0": CommandState.SUCCEEDED,
            "p1": CommandState.UNKNOWN,
            "p2": CommandState.UNKNOWN,
        }
        # 等被 shield 的底层任务把 p1/p2 真正写完（最多约 3s）。
        deadline = asyncio.get_running_loop().time() + 5.0
        while modbus_server.write_count < 3 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert modbus_server.write_count == 3
        # 收敛后终态仍为 UNKNOWN：物理成功不得覆盖安全裁决。
        states_after = {
            c.spec.point_id: (await control.get_command(c.command_id)).state for c in accepted
        }
        assert states_after == states_at_timeout
    finally:
        await executor.close(grace_seconds=10.0)

    verifier = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", modbus_server.port),
        table,
        {"word_order": "big_endian", "timeout": 2.0},
    )
    await verifier.connect()
    try:
        assert (await verifier.read_one("p0")).value == pytest.approx(111.25)
        assert (await verifier.read_one("p1")).value == pytest.approx(222.5)
        assert (await verifier.read_one("p2")).value == pytest.approx(333.75)
    finally:
        await verifier.close()
