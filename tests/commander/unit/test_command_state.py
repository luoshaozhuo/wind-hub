"""领域模型：状态机合法性与命令规约校验。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from commander.domain.command import (
    TERMINAL_STATES,
    CommandOutcome,
    CommandSpec,
    CommandState,
    CommandStateError,
    validate_transition,
)
from core.domain.sample_value import WritableScalar

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_FUTURE = _NOW + timedelta(minutes=5)


def test_command_state_members_match_design_enum() -> None:
    assert {state.value for state in CommandState} == {
        "ACCEPTED",
        "CONNECTING",
        "EXECUTING",
        "SUCCEEDED",
        "FAILED",
        "UNKNOWN",
        "INTERRUPTED",
        "EXPIRED",
        "CANCELLED",
    }
    assert {outcome.value for outcome in CommandOutcome} == {
        "CONFIRMED_SUCCESS",
        "CONFIRMED_FAILURE",
        "UNKNOWN",
        "NOT_SENT",
    }


@pytest.mark.parametrize(
    ("previous", "current", "outcome"),
    [
        (CommandState.ACCEPTED, CommandState.CONNECTING, None),
        (CommandState.ACCEPTED, CommandState.EXECUTING, None),
        (CommandState.CONNECTING, CommandState.EXECUTING, None),
        (CommandState.EXECUTING, CommandState.SUCCEEDED, CommandOutcome.CONFIRMED_SUCCESS),
        (CommandState.EXECUTING, CommandState.FAILED, CommandOutcome.CONFIRMED_FAILURE),
        (CommandState.EXECUTING, CommandState.UNKNOWN, CommandOutcome.UNKNOWN),
        (CommandState.EXECUTING, CommandState.EXPIRED, CommandOutcome.NOT_SENT),
        (CommandState.ACCEPTED, CommandState.EXPIRED, CommandOutcome.NOT_SENT),
        (CommandState.ACCEPTED, CommandState.CANCELLED, CommandOutcome.NOT_SENT),
        (CommandState.ACCEPTED, CommandState.INTERRUPTED, CommandOutcome.NOT_SENT),
        (CommandState.ACCEPTED, CommandState.FAILED, CommandOutcome.CONFIRMED_FAILURE),
        (CommandState.CONNECTING, CommandState.FAILED, CommandOutcome.CONFIRMED_FAILURE),
        (CommandState.CONNECTING, CommandState.CANCELLED, CommandOutcome.NOT_SENT),
        (CommandState.CONNECTING, CommandState.INTERRUPTED, CommandOutcome.NOT_SENT),
        # 串行批量中断后可证明未发送的后续点：T2 已提交但现场从未触达。
        (CommandState.EXECUTING, CommandState.INTERRUPTED, CommandOutcome.NOT_SENT),
    ],
)
def test_legal_transitions(
    previous: CommandState, current: CommandState, outcome: CommandOutcome | None
) -> None:
    validate_transition(previous, current, outcome)


@pytest.mark.parametrize("state", list(TERMINAL_STATES))
def test_terminal_states_have_no_outgoing_transitions(state: CommandState) -> None:
    with pytest.raises(CommandStateError):
        validate_transition(state, CommandState.FAILED, CommandOutcome.CONFIRMED_FAILURE)


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        # 不允许跳过 CONNECTING/EXECUTING 直接成功。
        (CommandState.ACCEPTED, CommandState.SUCCEEDED),
        (CommandState.ACCEPTED, CommandState.UNKNOWN),
        (CommandState.CONNECTING, CommandState.SUCCEEDED),
        (CommandState.CONNECTING, CommandState.UNKNOWN),
        # UNKNOWN 只能在执行证据模糊时由 EXECUTING 进入。
        # EXECUTING→INTERRUPTED/NOT_SENT 合法：串行批量中可证明未发送的
        # 后续点在 durable T2 之后按证据收尾（v1_1 表 4-11，round5 N1）。
        (CommandState.EXECUTING, CommandState.CANCELLED),
        (CommandState.EXECUTING, CommandState.CONNECTING),
        (CommandState.ACCEPTED, CommandState.ACCEPTED),
    ],
)
def test_illegal_transitions_rejected(previous: CommandState, current: CommandState) -> None:
    with pytest.raises(CommandStateError):
        validate_transition(previous, current, CommandOutcome.NOT_SENT)


@pytest.mark.parametrize(
    ("current", "outcome"),
    [
        (CommandState.SUCCEEDED, CommandOutcome.CONFIRMED_FAILURE),
        (CommandState.FAILED, CommandOutcome.CONFIRMED_SUCCESS),
        (CommandState.UNKNOWN, CommandOutcome.NOT_SENT),
        (CommandState.EXPIRED, CommandOutcome.UNKNOWN),
        (CommandState.CANCELLED, CommandOutcome.CONFIRMED_FAILURE),
        (CommandState.INTERRUPTED, CommandOutcome.UNKNOWN),
    ],
)
def test_terminal_state_requires_matching_outcome(
    current: CommandState, outcome: CommandOutcome
) -> None:
    # 从前置合法状态出发，outcome 不匹配仍必须拒绝。
    previous = (
        CommandState.EXECUTING
        if current
        in {
            CommandState.SUCCEEDED,
            CommandState.FAILED,
            CommandState.UNKNOWN,
            CommandState.EXPIRED,
        }
        else CommandState.ACCEPTED
    )
    with pytest.raises(CommandStateError):
        validate_transition(previous, current, outcome)


def test_non_terminal_transitions_must_not_carry_terminal_outcome() -> None:
    # CONNECTING/EXECUTING 是过程事件，validate 不强制 outcome；
    # 仓储层只允许终态携带 outcome，这里钉死领域枚举不引入“成功的过程态”。
    assert CommandState.CONNECTING not in TERMINAL_STATES
    assert CommandState.EXECUTING not in TERMINAL_STATES


def test_spec_accepts_scalar_values() -> None:
    for value in (True, 0, 1.5, "reset"):
        spec = CommandSpec("dev", "point", value, "ref", _FUTURE)
        assert spec.value == value


@pytest.mark.parametrize("value", [None, b"x", [1], {"v": 1}, object()])
def test_spec_rejects_non_scalar_values(value: object) -> None:
    with pytest.raises(ValueError):
        CommandSpec("dev", "point", cast(WritableScalar, value), "ref", _FUTURE)


def test_spec_rejects_nan_and_infinity() -> None:
    with pytest.raises(ValueError):
        CommandSpec("dev", "point", float("nan"), "ref", _FUTURE)
    with pytest.raises(ValueError):
        CommandSpec("dev", "point", float("inf"), "ref", _FUTURE)


def test_spec_rejects_blank_identity_fields() -> None:
    with pytest.raises(ValueError):
        CommandSpec(" ", "point", 1, "ref", _FUTURE)
    with pytest.raises(ValueError):
        CommandSpec("dev", " ", 1, "ref", _FUTURE)
    with pytest.raises(ValueError):
        CommandSpec("dev", "point", 1, " ", _FUTURE)


def test_spec_rejects_naive_expiry() -> None:
    with pytest.raises(ValueError):
        CommandSpec("dev", "point", 1, "ref", datetime(2026, 1, 1))


def test_spec_allows_missing_expiry() -> None:
    spec = CommandSpec("dev", "point", 1, "ref")
    assert spec.expires_at is None
