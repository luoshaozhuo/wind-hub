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
