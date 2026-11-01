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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OPEN（round5 N1-b 在真实 Modbus 链路上复现）：p1 发往不存在的 "
        "slave 3，从站不响应，驱动等待超时按连接级故障裁决 UNKNOWN；执行器"
        "中止批次后确定未调用的 p2 被误判 UNKNOWN 而非 INTERRUPTED/NOT_SENT。"
        "服务端 PDU 计数与寄存器回读同时证明 p2 物理未发送。与组件级 "
        "strict xfail 同根因。"
    ),
)
async def test_real_modbus_unknown_mid_batch_physical_boundary_and_per_point_terminal(
    modbus_server, control
) -> None:
    # p0/p2 在 unit 1；p1 在不存在的 unit 3：PDU 到达从站但无任何响应，
    # 驱动按"未知结果、可能已执行"裁决 UNKNOWN（非 CONFIRMED_FAILURE）。
    table = make_table(
        "modbus",
        make_point("p0", register_type="holding", address=200, data_type="float32"),
        make_point("p1", register_type="holding", address=202, data_type="float32", slave_id=3),
        make_point("p2", register_type="holding", address=204, data_type="float32"),
    )

    def factory(_device_id: str) -> ModbusDriver:
        return ModbusDriver(
            ConnectionEndpoint("127.0.0.1", modbus_server.port),
            table,
            {"word_order": "big_endian", "timeout": 0.6},
        )

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
        assert states == {
            "p0": CommandState.SUCCEEDED,
            "p1": CommandState.UNKNOWN,
            "p2": CommandState.INTERRUPTED,
        }
    finally:
        await executor.close()

    # 物理证据：p0 真实落寄存器；p1 无响应寄存器保持原值；
    # p2 的 FC06 从未发出（服务端仅收到 p0/p1 两个写 PDU）。
    assert modbus_server.write_count == 2
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
