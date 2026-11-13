"""SimulationController 单元测试：去重、设备 FIFO、证据提升与 UNKNOWN 隔离。

不触碰 gRPC 与 mTLS：SimulationInputPort 全部使用进程内脚本替身，
精确控制 submit/get_execution 的返回与异常。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from industrial_gateway.application.ports.simulation import SimulationInputReceipt
from industrial_gateway.application.simulation import (
    SimulationCommand,
    SimulationController,
)

pytestmark = pytest.mark.unit


class FakePort:
    """记录调用顺序并按脚本返回 submit/get_execution 结果。"""

    def __init__(
        self,
        *,
        receipt: SimulationInputReceipt | None = None,
        submit_error: Exception | None = None,
        evidence: list[str] | None = None,
        evidence_error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.receipt = receipt or SimulationInputReceipt(
            accepted=True,
            effective_step=7,
            generation_id="gen-7",
            message="",
            error_code="",
        )
        self.submit_error = submit_error
        self.evidence = list(evidence or [])
        self.evidence_error = evidence_error
        self.delay = delay
        self.submissions: list[tuple[str, str]] = []
        self.queries: list[tuple[str, str]] = []
        self.closed = 0
        self.started = 0

    def start(self) -> None:
        self.started += 1

    async def submit(self, **kwargs: Any) -> SimulationInputReceipt:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.submissions.append((kwargs["device_id"], kwargs["request_id"]))
        if self.submit_error is not None:
            raise self.submit_error
        return self.receipt

    async def get_execution(self, **kwargs: Any) -> str:
        self.queries.append((kwargs["generation_id"], kwargs["request_id"]))
        if self.evidence_error is not None:
            raise self.evidence_error
        if self.evidence:
            return self.evidence.pop(0)
        return "APPLIED"

    async def close(self) -> None:
        self.closed += 1


def _command(
    request_id: str = "req-1",
    *,
    expires_in: float = 5.0,
    point_id: str = "power_limit_setpoint",
) -> SimulationCommand:
    return SimulationCommand(
        caller="commander-cn",
        gateway_id="gw-1",
        device_id="WTG_SIM01",
        point_id=point_id,
        value=1500.0,
        request_id=request_id,
        expected_revision="rev-1",
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )


async def test_accepted_then_pending_then_applied_evidence_promotion() -> None:
    port = FakePort(evidence=["PENDING", "APPLIED"])
    controller = SimulationController(lambda _gateway_id: port)

    outcome = await controller.execute(_command())
    assert outcome.accepted is True
    assert outcome.state == "ACCEPTED"
    assert outcome.effective_step == 7
    assert outcome.generation_id == "gen-7"

    first = await controller.result(caller="commander-cn", gateway_id="gw-1", request_id="req-1")
    assert first.accepted is True
    assert first.state == "PENDING"
    second = await controller.result(caller="commander-cn", gateway_id="gw-1", request_id="req-1")
    assert second.accepted is True
    assert second.state == "APPLIED"
    assert second.effective_step == 7
    # 查询必须携带受理时记录的 generation 身份与原 request_id。
    assert port.queries == [("gen-7", "req-1"), ("gen-7", "req-1")]
    assert port.closed == 3  # submit 通道与两次查询通道各自关闭


async def test_generator_unknown_evidence_and_query_failure_never_become_applied() -> None:
    unknown_port = FakePort(evidence=["UNKNOWN"])
    controller = SimulationController(lambda _gateway_id: unknown_port)
    await controller.execute(_command("req-unknown"))
    outcome = await controller.result(
        caller="commander-cn", gateway_id="gw-1", request_id="req-unknown"
    )
    assert outcome.accepted is True
    assert outcome.state == "UNKNOWN"
    assert outcome.error_code == "EXECUTION_NOT_CONFIRMED"

    broken_port = FakePort(evidence_error=ConnectionError("broker reset"))
    controller_broken = SimulationController(lambda _gateway_id: broken_port)
    await controller_broken.execute(_command("req-broken"))
    outcome = await controller_broken.result(
        caller="commander-cn", gateway_id="gw-1", request_id="req-broken"
    )
    assert outcome.accepted is True
    assert outcome.state == "UNKNOWN"
    assert outcome.error_code == "EXECUTION_QUERY_FAILED"


async def test_same_identity_replays_original_task_without_second_submission() -> None:
    port = FakePort(delay=0.05)
    controller = SimulationController(lambda _gateway_id: port)
    command = _command("req-replay")
    first = asyncio.create_task(controller.execute(command))
    await asyncio.sleep(0.005)
    second = await controller.execute(command)
    first_outcome = await first
    assert first_outcome == second
    assert port.submissions == [("WTG_SIM01", "req-replay")]


async def test_same_request_id_with_different_payload_is_conflict() -> None:
    port = FakePort()
    controller = SimulationController(lambda _gateway_id: port)
    await controller.execute(_command("req-dup"))
    conflicting = _command("req-dup", point_id="agc_power_setpoint")
    outcome = await controller.execute(conflicting)
    assert outcome.accepted is False
    assert outcome.state == "REJECTED"
    assert outcome.error_code == "IDEMPOTENCY_CONFLICT"
    assert port.submissions == [("WTG_SIM01", "req-dup")]


async def test_unknown_identity_and_foreign_caller_cannot_observe_evidence() -> None:
    port = FakePort()
    controller = SimulationController(lambda _gateway_id: port)
    await controller.execute(_command("req-owned"))
    missing = await controller.result(
        caller="commander-cn", gateway_id="gw-1", request_id="req-missing"
    )
    assert missing.state == "UNKNOWN"
    assert missing.error_code == "EXECUTION_NOT_OBSERVED"
    foreign = await controller.result(
        caller="other-commander-cn", gateway_id="gw-1", request_id="req-owned"
    )
    assert foreign.state == "UNKNOWN"
    assert foreign.error_code == "EXECUTION_NOT_OBSERVED"
    assert port.queries == []


async def test_same_device_commands_are_serialized_fifo_across_controllers() -> None:
    order: list[str] = []

    class OrderPort(FakePort):
        async def submit(self, **kwargs: Any) -> SimulationInputReceipt:
            order.append(f"enter:{kwargs['request_id']}")
            await asyncio.sleep(0.05)
            order.append(f"leave:{kwargs['request_id']}")
            return await super().submit(**kwargs)

    controller = SimulationController(lambda _gateway_id: OrderPort())
    results = await asyncio.gather(
        controller.execute(_command("req-a")),
        controller.execute(_command("req-b")),
    )
    assert [outcome.state for outcome in results] == ["ACCEPTED", "ACCEPTED"]
    assert order == [
        "enter:req-a",
        "leave:req-a",
        "enter:req-b",
        "leave:req-b",
    ]


async def test_expired_command_and_rejected_receipt_and_transport_failure() -> None:
    expired_port = FakePort()
    controller = SimulationController(lambda _gateway_id: expired_port)
    expired = await controller.execute(_command("req-expired", expires_in=-1.0))
    assert expired.accepted is False
    assert expired.state == "REJECTED"
    assert expired.error_code == "EXPIRED"
    assert expired_port.submissions == []

    rejected_port = FakePort(
        receipt=SimulationInputReceipt(
            accepted=False,
            effective_step=0,
            generation_id="",
            message="bad point",
            error_code="INPUT_REJECTED",
        )
    )
    controller_rejected = SimulationController(lambda _gateway_id: rejected_port)
    rejected = await controller_rejected.execute(_command("req-rejected"))
    assert rejected.accepted is False
    assert rejected.state == "REJECTED"
    assert rejected.error_code == "INPUT_REJECTED"

    transport_port = FakePort(submit_error=ConnectionError("connection lost"))
    controller_failed = SimulationController(lambda _gateway_id: transport_port)
    ambiguous = await controller_failed.execute(_command("req-lost"))
    # 传输歧义按可能已送达处理：accepted=True 但只能是 UNKNOWN，禁止盲重试。
    assert ambiguous.accepted is True
    assert ambiguous.state == "UNKNOWN"
    assert ambiguous.error_code == "ConnectionError"

    value_error_port = FakePort(submit_error=ValueError("generation changed"))
    controller_value = SimulationController(lambda _gateway_id: value_error_port)
    deterministic = await controller_value.execute(_command("req-value"))
    assert deterministic.accepted is False
    assert deterministic.state == "REJECTED"
    assert deterministic.error_code == "ValueError"


async def test_missing_effective_step_is_unknown_not_applied() -> None:
    port = FakePort(
        receipt=SimulationInputReceipt(
            accepted=True,
            effective_step=0,
            generation_id="gen-9",
            message="",
            error_code="",
        )
    )
    controller = SimulationController(lambda _gateway_id: port)
    outcome = await controller.execute(_command("req-no-step"))
    assert outcome.accepted is True
    assert outcome.state == "UNKNOWN"
    assert outcome.error_code == "NO_EFFECTIVE_STEP"
    # 无 effective_step 时不会发起证据查询。
    assert port.queries == []


async def test_capacity_limit_rejects_new_identities() -> None:
    port = FakePort(delay=0.05)
    controller = SimulationController(lambda _gateway_id: port, capacity=1)
    await controller.execute(_command("req-only"))
    outcome = await controller.execute(_command("req-overflow"))
    assert outcome.accepted is False
    assert outcome.error_code == "EXECUTION_CAPACITY_EXCEEDED"
