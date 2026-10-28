"""即时写命令领域模型 application/command.py 的独立契约测试。

CommandResult 与 Web/Commander 路径有大量间接使用，但命令模型本身的
不变量需要独立验证：幂等键、允许标量类型、UTC 时间戳、UNKNOWN 语义。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from core.application.command import Command, CommandResult
from core.application.protocol_contract import WriteOutcome


@pytest.mark.parametrize("value", [True, 42, -7, 3.14, "reset"])
def test_command_accepts_declared_scalar_values(value) -> None:
    command = Command(
        command_id="cmd-1",
        device_id="dev-1",
        point_id="setpoint",
        value=value,
    )

    assert command.value == value
    assert command.timeout == 5.0
    assert command.issued_at.tzinfo is not None


def test_command_timestamps_are_utc_by_default() -> None:
    before = datetime.now(UTC)
    command = Command("c", "d", "p", 1)
    after = datetime.now(UTC)

    assert before <= command.issued_at <= after


def test_command_keeps_caller_supplied_id_and_timeout() -> None:
    issued = datetime(2030, 1, 1, tzinfo=UTC)
    command = Command(
        command_id="fixed-id",
        device_id="d",
        point_id="p",
        value=1,
        timeout=0.0,
        issued_at=issued,
    )

    # timeout <= 0 表示「用进程默认」，由命令模型原样透传，不擅自改写。
    assert command.timeout == 0.0
    assert command.command_id == "fixed-id"
    assert command.issued_at is issued


@pytest.mark.parametrize("outcome", list(WriteOutcome))
def test_command_result_echoes_id_and_outcome(outcome: WriteOutcome) -> None:
    result = CommandResult(command_id="cmd-9", outcome=outcome)

    assert result.command_id == "cmd-9"
    assert result.outcome is outcome
    assert result.error is None
    assert result.finished_at.tzinfo is not None


def test_command_result_carries_error_for_non_success() -> None:
    result = CommandResult(
        command_id="cmd-10",
        outcome=WriteOutcome.CONFIRMED_FAILURE,
        error="no acknowledgement",
    )

    assert result.error == "no acknowledgement"


def test_command_is_frozen() -> None:
    command = Command("c", "d", "p", 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.value = 2  # type: ignore[misc]
