"""Commander 三态控制契约。"""

from __future__ import annotations

import pytest

from core.infrastructure.rpc import commander_pb2 as pb
from industrial_gateway.application.command_service import (
    CommandState,
    CommandTarget,
    CommanderProxy,
)
from industrial_gateway.domain import PointWrite


class FakeCommander:
    def __init__(self, outcome: int) -> None:
        self.outcome = outcome
        self.requests: list[pb.WritePointRequest] = []

    async def WritePoint(  # noqa: N802
        self, request: pb.WritePointRequest, *, timeout: float
    ) -> pb.CommandResultMessage:
        self.requests.append(request)
        return pb.CommandResultMessage(command_id=request.command_id, outcome=self.outcome)


async def test_confirmed_success_allows_repeated_same_value() -> None:
    fake = FakeCommander(pb.COMMAND_OUTCOME_CONFIRMED_SUCCESS)
    proxy = CommanderProxy(
        fake, {("t1", "command"): CommandTarget("physical", "setpoint")}
    )
    command = PointWrite("t1", "command", 42, "dc-ems")
    await proxy(command)
    await proxy(command)
    assert len(fake.requests) == 2
    assert fake.requests[0].command_id != fake.requests[1].command_id
    assert fake.requests[0].device_id == "physical"
    assert proxy.state("t1", "command") is CommandState.READY


async def test_unknown_blocks_subsequent_writes() -> None:
    fake = FakeCommander(pb.COMMAND_OUTCOME_UNKNOWN)
    proxy = CommanderProxy(fake, {("t1", "command"): CommandTarget("physical", "setpoint")})
    command = PointWrite("t1", "command", 42, "collector")
    with pytest.raises(RuntimeError, match="unknown"):
        await proxy(command)
    assert proxy.state("t1", "command") is CommandState.UNKNOWN
    with pytest.raises(RuntimeError, match="locked"):
        await proxy(command)
    assert len(fake.requests) == 1


async def test_confirmed_failure_does_not_lock() -> None:
    fake = FakeCommander(pb.COMMAND_OUTCOME_CONFIRMED_FAILURE)
    proxy = CommanderProxy(fake, {("t1", "command"): CommandTarget("physical", "setpoint")})  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="rejected"):
        await proxy(PointWrite("t1", "command", 2, "dc-ems"))
    assert proxy.state("t1", "command") is CommandState.READY
