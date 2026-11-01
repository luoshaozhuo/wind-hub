"""Commander 组件测试共享构造：真实 SQLite 仓储 + 受控伪协议会话。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from commander.application.control import CommandControl
from commander.application.queued_executor import QueuedExecutor
from commander.domain.command import AcceptedCommand, CommandResult, CommandSpec
from core.application.protocol_contract import (
    ProtocolCapability,
    ProtocolWrite,
    ProtocolWriteResult,
    WriteOutcome,
)
from core.domain.sample_value import WritableScalar


class FakeSession:
    """记录调用、可注入延迟/故障的协议会话。"""

    def __init__(
        self,
        *,
        capabilities: frozenset[ProtocolCapability] = frozenset(
            {ProtocolCapability.READ, ProtocolCapability.WRITE}
        ),
        connect_delay: float = 0.0,
        connect_error: Exception | None = None,
        write_delay: float = 0.0,
        write_error: Exception | None = None,
        outcome: WriteOutcome = WriteOutcome.CONFIRMED_SUCCESS,
        close_delay: float = 0.0,
        close_error: Exception | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._connect_delay = connect_delay
        self._connect_error = connect_error
        self._write_delay = write_delay
        self._write_error = write_error
        self._outcome = outcome
        self._close_delay = close_delay
        self._close_error = close_error
        self.open = False
        self.connects = 0
        self.closes = 0
        self.one_calls: list[str] = []
        self.many_calls: list[tuple[str, ...]] = []

    async def connect(self) -> None:
        self.connects += 1
        if self._connect_delay:
            await asyncio.sleep(self._connect_delay)
        if self._connect_error is not None:
            raise self._connect_error
        self.open = True

    def is_open(self) -> bool:
        return self.open

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return self._capabilities

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        self.one_calls.append(write.point)
        if self._write_delay:
            await asyncio.sleep(self._write_delay)
        if self._write_error is not None:
            raise self._write_error
        return ProtocolWriteResult(write.point, self._outcome)

    async def write_many(self, writes: Sequence[ProtocolWrite]) -> tuple[ProtocolWriteResult, ...]:
        points = tuple(write.point for write in writes)
        self.many_calls.append(points)
        if self._write_delay:
            await asyncio.sleep(self._write_delay)
        if self._write_error is not None:
            raise self._write_error
        return tuple(ProtocolWriteResult(point, self._outcome) for point in points)

    async def close(self) -> None:
        self.closes += 1
        if self._close_delay:
            await asyncio.sleep(self._close_delay)
        if self._close_error is not None:
            raise self._close_error
        self.open = False


def make_spec(
    point: str = "setpoint",
    *,
    device: str = "WT01",
    value: WritableScalar = 12.5,
    config_reference: str = "ref-abc",
) -> CommandSpec:
    return CommandSpec(device, point, value, config_reference)


def prepare_write(spec: CommandSpec) -> ProtocolWrite:
    return ProtocolWrite(point=spec.point_id, value=spec.value)


async def wait_for_result(
    executor: QueuedExecutor, command: AcceptedCommand, *, timeout: float = 2.0
) -> CommandResult:
    result = await asyncio.wait_for(executor.wait(command.command_id), timeout=timeout)
    assert result is not None
    return result


def build_executor(
    control: CommandControl,
    sessions: Mapping[str, FakeSession],
    **kwargs: Any,
) -> QueuedExecutor:
    return QueuedExecutor(
        control,
        lambda device_id: sessions[device_id],
        lambda _spec: True,
        prepare_write,
        **kwargs,
    )
