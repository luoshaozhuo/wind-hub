"""Real-Redis contract tests for the Gate-5 batch Lua seams (tests 9-10).

9. one server ``TIME`` clock per EVAL: expired and live points in the same
   batch are adjudicated together, with exactly one EVAL on the wire.
10. a full 500-point batch mixes first writes, duplicate confirms,
    out-of-order rejection and same-timestamp conflicts while preserving
    the existing business versions in Redis.

No mocks: the external R5 stack is supplied via SINK_R5_* environment
variables; otherwise the fixture owns the pinned docker lifecycle.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from sink_service.application.delivery_point import DeliveryPoint
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.realtime_adapters import RedisOutput
from tests.fixtures.services.r5_stack import (
    R5Stack,
    R5StackError,
    docker_available,
    down_stack,
    up_stack,
)

pytestmark = [pytest.mark.integration]

FARM = "farm-a"
COLLECTOR = "collector-1"


def _sample(
    sample_id: str,
    *,
    device: str,
    when: datetime,
    staleness: int | None,
    value: float = 1.0,
    quality: str = "good",
) -> DeliveryPoint:
    now = datetime.now(UTC)
    return DeliveryPoint(
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id="task-1",
        device_id=device,
        point_id="active_power",
        sample_id=sample_id,
        value=value,
        quality=quality,  # type: ignore[arg-type]
        sample_time=when,
        publish_time=now,
        receive_time=None,
        max_staleness_ms=staleness,
    )


@pytest.fixture
def stack() -> R5Stack:
    external = os.environ.get("SINK_R5_NATS")
    if external:
        yield R5Stack(
            project=os.environ.get("SINK_R5_PROJECT", "wind-hub-r5-manual"),
            nats=external,
            redis=os.environ["SINK_R5_REDIS"],
            tdengine_rest=os.environ["SINK_R5_TDENGINE"],
            kafka=os.environ["SINK_R5_KAFKA"],
            nats_monitor_port=int(os.environ["SINK_R5_NATS_MON_PORT"]),
        )
        return
    if not docker_available():
        pytest.skip("docker daemon unavailable; real-Redis batch tests cannot run")
    value = up_stack()
    try:
        yield value
    finally:
        down_stack(value)


@pytest.fixture
async def redis_output(stack: R5Stack):
    # A run-unique namespace isolates the tests without flushing the shared
    # real Redis instance used by the rest of the R5 stack.
    namespace = f"g5batch-{uuid.uuid4().hex[:12]}"
    host, _, port = stack.redis.rpartition(":")
    output = RedisOutput(
        host=host,
        port=int(port),
        namespace=namespace,
        timeout_ms=2000,
    )
    await output.open()
    try:
        yield output, stack, namespace
    finally:
        await output.close()


def _eval_calls(stack: R5Stack) -> int:
    result = stack.docker(
        "exec",
        stack.container("redis"),
        "redis-cli",
        "INFO",
        "commandstats",
    )
    if result.returncode != 0:
        raise R5StackError(f"redis-cli INFO failed: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        if line.startswith("cmdstat_eval:"):
            payload = line.split(":", 1)[1]
            for part in payload.split(","):
                if part.startswith("calls="):
                    return int(part.split("=", 1)[1])
    return 0


def _redis_cli(stack: R5Stack, *arguments: str) -> str:
    result = stack.docker("exec", stack.container("redis"), "redis-cli", *arguments)
    if result.returncode != 0:
        raise R5StackError(f"redis-cli {arguments} failed: {result.stderr.strip()}")
    return result.stdout.strip()


async def test_shared_server_time_clock_adjudicates_one_batch_with_one_eval(
    redis_output,
) -> None:
    output, stack, namespace = redis_output
    now = datetime.now(UTC)
    expired = _sample(
        "expired-1",
        device="WT001",
        when=now - timedelta(seconds=10),
        staleness=1000,
    )
    live = _sample("live-1", device="WT002", when=now, staleness=5000)
    boundary = _sample(
        "boundary-1",
        device="WT003",
        when=now,
        staleness=2000,
        value=2.5,
    )

    before = _eval_calls(stack)
    outcome = await output.write_batch((expired, live, boundary))
    after = _eval_calls(stack)

    # Exactly one EVAL carries the whole batch: the shared TIME clock is
    # evaluated once per script invocation, not once per key.
    assert after - before == 1
    assert outcome.settled == (0, 1, 2)
    assert outcome.delivered == (1, 2)
    assert outcome.error == ""

    expired_key = ":".join((namespace, FARM, COLLECTOR, "WT001", "active_power"))
    live_key = ":".join((namespace, FARM, COLLECTOR, "WT002", "active_power"))
    boundary_key = ":".join((namespace, FARM, COLLECTOR, "WT003", "active_power"))
    assert _redis_cli(stack, "GET", expired_key) == ""
    assert '"sample_id":"live-1"' in _redis_cli(stack, "GET", live_key)
    boundary_ttl = int(_redis_cli(stack, "PTTL", boundary_key))
    assert 0 < boundary_ttl <= 2000


async def test_full_500_point_batch_mixes_confirm_reject_conflict_and_writes(
    redis_output,
) -> None:
    output, stack, namespace = redis_output
    base = datetime.now(UTC)
    t1 = base - timedelta(milliseconds=300)
    t2 = base - timedelta(milliseconds=200)
    t3 = base - timedelta(milliseconds=100)

    # Pre-seed the three adjudicated identities.
    seed = (
        _sample("dup-1", device="WT000", when=t1, staleness=60000),
        _sample("new-1", device="WT001", when=t2, staleness=60000),
        _sample("conf-1", device="WT002", when=t3, staleness=60000),
    )
    seed_outcome = await output.write_batch(seed)
    assert seed_outcome.delivered == (0, 1, 2)

    batch: list[DeliveryPoint] = [
        # code 2: identical identity re-confirm, counted as delivered.
        _sample("dup-1", device="WT000", when=t1, staleness=60000),
        # code 0: older sample_time must not roll the stored version back.
        _sample("old-1", device="WT001", when=t1, staleness=60000),
        # code -1: same sample_time with a different sample_id is a conflict.
        _sample("conf-2", device="WT002", when=t3, staleness=60000),
    ]
    # code 1: the remaining 497 points are first writes.
    for index in range(3, 500):
        batch.append(
            _sample(
                f"fresh-{index}",
                device=f"WT{index:03d}",
                when=base,
                staleness=60000,
                value=float(index),
            )
        )
    assert len(batch) == 500

    outcome = await output.write_batch(tuple(batch))

    assert len(outcome.settled) == 500
    assert 0 in outcome.delivered  # duplicate confirm
    assert 1 not in outcome.delivered  # out-of-order rejection
    assert 2 not in outcome.delivered  # same-timestamp conflict
    assert set(outcome.delivered) == {0, *range(3, 500)}
    assert "2" in outcome.error
    assert outcome.failed_attempts == 0
    assert outcome.successful_attempts == 1

    key_wt001 = ":".join((namespace, FARM, COLLECTOR, "WT001", "active_power"))
    key_wt002 = ":".join((namespace, FARM, COLLECTOR, "WT002", "active_power"))
    assert '"sample_id":"new-1"' in _redis_cli(stack, "GET", key_wt001)
    assert '"sample_id":"conf-1"' in _redis_cli(stack, "GET", key_wt002)
    key_wt499 = ":".join((namespace, FARM, COLLECTOR, "WT499", "active_power"))
    assert '"sample_id":"fresh-499"' in _redis_cli(stack, "GET", key_wt499)

    # The 501st point in a single batch is a hard adapter guard.
    with pytest.raises(ConfigurationError, match="exceeds 500 points"):
        await output.write_batch(
            tuple(
                _sample(
                    f"over-{index}",
                    device=f"OV{index:03d}",
                    when=base,
                    staleness=60000,
                )
                for index in range(501)
            )
        )
