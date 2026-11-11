"""R13（v1_47）Kafka Builder 尝试计数器单位钉死。

设计 v1_47 §3.5/§14.7.2 的唯一口径：
- ``successful_attempts`` 按得到可信确认的 Builder Future 数计（I/O 尝试次），
  不按 Record 数；Record 口径只属于 ``delivered_total``（由 delivered 索引体现）。
- ``failed_attempts`` 对未排队（send_batch 拒绝）与未确认（Future 拒绝）的
  每次尝试各计一次；UNKNOWN/未收敛绝不能计为成功。

fake 基建复用 tests/src/unit/test_b2_batch_builder.py，与
test_kafka_encoded_batch.py 同一套 producer 表面，不引入第二套 mock。
"""

from __future__ import annotations

from typing import Any

import pytest

from sink_service.infrastructure.realtime_adapters import KafkaOutput
from tests.src.unit.test_b2_batch_builder import (
    KEY,
    STRIPE,
    _output,
    _payloads,
    build_producer_env,
)

pytestmark = [pytest.mark.unit, pytest.mark.reliability, pytest.mark.kafka]


@pytest.fixture
def producer_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return build_producer_env(monkeypatch)


async def test_success_counts_builders_not_records(producer_env: dict[str, Any]) -> None:
    # 容量 100、250 点 => 3 个 Builder Future，250 条 Record。
    producer_env["capacity"]["value"] = 100
    output = _output()
    await output.open()
    try:
        outcome = await output.write_encoded_batch(_payloads(250), key=KEY, stripe=STRIPE)
        assert outcome.error == ""
        assert len(outcome.delivered) == 250
        assert outcome.successful_attempts == 3
        assert outcome.failed_attempts == 0
        # 每个确认 Builder 留下一条带权重的延迟样本，权重=该 Builder 的 Record 数。
        assert sum(weight for _, weight in outcome.successful_latency_weights) == 250
    finally:
        await output.close()


async def test_single_builder_success_is_one_attempt(producer_env: dict[str, Any]) -> None:
    output = _output()
    await output.open()
    try:
        outcome = await output.write_encoded_batch(_payloads(5), key=KEY, stripe=STRIPE)
        assert outcome.delivered == (0, 1, 2, 3, 4)
        assert outcome.successful_attempts == 1
        assert outcome.failed_attempts == 0
    finally:
        await output.close()


async def test_confirmation_rejection_counts_every_failed_future(
    producer_env: dict[str, Any],
) -> None:
    producer_env["capacity"]["value"] = 100
    producer_env["flags"]["confirm_fails"] = True
    output = _output()
    await output.open()
    try:
        outcome = await output.write_encoded_batch(_payloads(250), key=KEY, stripe=STRIPE)
        assert outcome.delivered == ()
        assert "not confirmed" in outcome.error
        # 三个 Builder 全部被 broker 拒绝：失败尝试 3 次、成功 0 次，
        # 不允许把 UNKNOWN/拒绝当成功，也不允许遗弃 Future。
        assert outcome.failed_attempts == 3
        assert outcome.successful_attempts == 0
    finally:
        await output.close()


async def test_send_batch_rejection_counts_one_failed_attempt(
    producer_env: dict[str, Any],
) -> None:
    producer_env["capacity"]["value"] = 100
    flags = producer_env["flags"]
    call_count = {"n": 0}

    def fail_second_submission() -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            flags["send_batch_fails"] = True

    producer_env["gates"]["on_send_batch"] = fail_second_submission
    output = _output()
    await output.open()
    try:
        outcome = await output.write_encoded_batch(_payloads(250), key=KEY, stripe=STRIPE)
        # 首个 Builder [0,100) 已确认；第二个提交被拒后 fail-closed 停止。
        assert outcome.delivered == tuple(range(100))
        assert "not queued" in outcome.error
        assert outcome.failed_attempts == 1
        assert outcome.successful_attempts == 1
    finally:
        await output.close()


async def test_disconnected_output_keeps_counters_at_zero(
    producer_env: dict[str, Any],
) -> None:
    output: KafkaOutput = _output()
    outcome = await output.write_encoded_batch(_payloads(3), key=KEY, stripe=STRIPE)
    assert outcome.error == "Kafka output is not connected"
    assert outcome.delivered == ()
    assert outcome.successful_attempts == 0
    assert outcome.failed_attempts == 0
