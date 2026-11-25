"""SinkOutputPool 三层额度与共享 Producer 语义（组件层，fake adapter）。

覆盖设计 §6.3.1 / §6.3.3：

- 并发模板：Sink 额度取 runtime 与 max_safe_inflight 的交集；
- 旧模板：额度强制为 1，保持串行；
- 同 Sink 多次 acquire 共享同一 adapter 并累计引用，末位 release 才关闭；
- 不同设备的两个 _QuotaWriter 可真正并行，单 Writer 在途重入被拒；
- 全局额度收紧到 1 时，并行写入被串行化（三层额度的最内层生效）。
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from sink_service.application.realtime import BatchOutcome
from sink_service.domain.config import ValidatedSnapshot
from sink_service.infrastructure import realtime_runtime
from sink_service.infrastructure.config_io import validate_snapshot
from sink_service.infrastructure.realtime_runtime import SinkOutputPool
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    SERVICE_FILE,
    SINKS_INSTANCE,
    TEMPLATE_ROOT,
    service_document,
    snapshot_files,
)

pytestmark = [pytest.mark.component, pytest.mark.reliability]

KAFKA_110 = f"{TEMPLATE_ROOT}/sinks/sink_kafka_history_v1.1.0.yaml"
KAFKA_100 = f"{TEMPLATE_ROOT}/sinks/sink_kafka_history_v1.0.0.yaml"


def _kafka_template(*, version: str, concurrent: bool, inflight: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "file_version": version,
        "template_id": "kafka_history",
        "kind": "kafka",
        "capabilities": {
            "realtime": False,
            "reliable": True,
            "idempotent": False,
            "supports_concurrent_write": concurrent,
            "max_safe_inflight": inflight,
        },
        "connection": {"bootstrap_servers": ["192.168.1.20:9092"]},
        "output": {
            "topic": "windhub.samples",
            "key_field": "device_id",
            "business_duplicate_policy": "allow_at_least_once",
            "dedup_identity_fields": ["delivery_id", "sample_id"],
            "enable_idempotence": True,
            "acks": "all",
            "write_timeout_ms": 2000,
            "linger_ms": 0,
        },
    }


def _snapshot(
    *,
    version: str = "1.1.0",
    concurrent: bool = True,
    inflight: int = 4,
    global_quota: int = 128,
    per_sink: int = 8,
) -> ValidatedSnapshot:
    service = service_document()
    service["runtime"]["max_inflight_global"] = global_quota
    service["runtime"]["max_inflight_per_sink"] = per_sink
    docs = {
        SERVICE_FILE: service,
        KAFKA_110 if concurrent else KAFKA_100: _kafka_template(
            version=version, concurrent=concurrent, inflight=inflight
        ),
        SINKS_INSTANCE: {
            "schema_version": 1,
            "file_version": "1.0.0",
            "revision": "sinks-r7",
            "sinks": {
                "history_kafka": {
                    "template_id": "kafka_history",
                    "template_version": version,
                    "connection": {"bootstrap_servers": ["192.168.1.20:9092"]},
                    "overrides": {},
                }
            },
        },
        DELIVERIES_INSTANCE: {
            "schema_version": 1,
            "file_version": "1.0.0",
            "revision": "deliveries-r7",
            "deliveries": {},
        },
    }
    return validate_snapshot(snapshot_files(docs))


class FakeKafkaAdapter:
    """记录在途并发度的可门控 fake；按实例统计开关次数。"""

    instances: list[FakeKafkaAdapter] = []

    def __init__(self, **kwargs: Any) -> None:
        self.opened = 0
        self.closed = 0
        self.inflight = 0
        self.max_inflight = 0
        self.writes = 0
        self._gate: asyncio.Event | None = None
        FakeKafkaAdapter.instances.append(self)

    async def open(self) -> None:
        self.opened += 1

    async def close(self) -> None:
        self.closed += 1

    def block_next_write(self) -> asyncio.Event:
        self._gate = asyncio.Event()
        return self._gate

    async def write_batch(self, samples: tuple[Any, ...]) -> BatchOutcome:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        self.writes += 1
        try:
            if self._gate is not None and not self._gate.is_set():
                await self._gate.wait()
            return BatchOutcome(
                delivered=tuple(range(len(samples))),
                settled=tuple(range(len(samples))),
                successful_attempts=len(samples),
            )
        finally:
            self.inflight -= 1

    async def write_encoded_batch(
        self, payloads: tuple[bytes, ...], *, key: bytes, stripe: int
    ) -> BatchOutcome:
        self.encoded_writes = getattr(self, "encoded_writes", 0) + 1
        self.encoded_last = (payloads, key, stripe)
        return await self.write_batch(payloads)

    async def settle(self) -> BatchOutcome | None:
        return None


@pytest.fixture
def fake_kafka(monkeypatch: pytest.MonkeyPatch):
    FakeKafkaAdapter.instances = []
    monkeypatch.setattr(realtime_runtime, "KafkaOutput", FakeKafkaAdapter)
    return FakeKafkaAdapter


async def test_concurrent_sink_quota_is_intersection_and_adapter_shared(
    fake_kafka: type[FakeKafkaAdapter],
) -> None:
    snapshot = _snapshot(global_quota=128, per_sink=8)
    pool = SinkOutputPool()
    writer_a = await pool.acquire("history_kafka", snapshot)
    writer_b = await pool.acquire("history_kafka", snapshot)
    try:
        entry = pool._entries["history_kafka"]  # noqa: SLF001
        assert entry.quota.limit - entry.quota.inflight == 4  # noqa: SLF001  min(8, max_safe_inflight=4)
        assert entry.references == 2
        assert entry.adapter is writer_a._adapter is writer_b._adapter  # noqa: SLF001
        assert fake_kafka.instances[0].opened == 1
    finally:
        await pool.release("history_kafka")
        await pool.release("history_kafka")
    assert "history_kafka" not in pool._entries  # noqa: SLF001
    assert fake_kafka.instances[0].closed == 1


async def test_distinct_sink_ids_share_one_physical_target_quota(
    fake_kafka: type[FakeKafkaAdapter],
) -> None:
    snapshot = _snapshot(inflight=2)
    snapshot.sinks["history_kafka_b"] = copy.deepcopy(snapshot.sinks["history_kafka"])
    pool = SinkOutputPool()
    writer_a = await pool.acquire("history_kafka", snapshot)
    writer_b = await pool.acquire("history_kafka_b", snapshot)
    entry_a = pool._entries["history_kafka"]  # noqa: SLF001
    entry_b = pool._entries["history_kafka_b"]  # noqa: SLF001
    try:
        assert entry_a.physical is entry_b.physical
        assert entry_a.physical.references == 2
        assert len(pool._physical_quotas) == 1  # noqa: SLF001
        assert writer_a._sink_quota is writer_b._sink_quota  # noqa: SLF001
    finally:
        await pool.release("history_kafka")
        assert entry_a.physical.references == 1
        await pool.release("history_kafka_b")
    assert pool._physical_quotas == {}  # noqa: SLF001


async def test_dynamic_minimum_physical_quota_shrinks_and_restores(
    fake_kafka: type[FakeKafkaAdapter],
) -> None:
    """Cross-Sink caps shrink below in-flight work without revoking that work."""
    snapshot = _snapshot(inflight=4)
    pool = SinkOutputPool()
    writer_a1 = await pool.acquire("history_kafka", snapshot)
    writer_a2 = await pool.acquire("history_kafka", snapshot)
    adapter_a = fake_kafka.instances[0]
    gate = adapter_a.block_next_write()
    first = asyncio.create_task(writer_a1.write_batch(("a1",)))
    second = asyncio.create_task(writer_a2.write_batch(("a2",)))
    while adapter_a.inflight < 2:
        await asyncio.sleep(0.005)

    snapshot.sinks["history_kafka_b"] = copy.deepcopy(snapshot.sinks["history_kafka"])
    snapshot.documents[KAFKA_110]["capabilities"]["max_safe_inflight"] = 1
    await pool.acquire("history_kafka_b", snapshot)
    snapshot.sinks["history_kafka_c"] = copy.deepcopy(snapshot.sinks["history_kafka"])
    snapshot.documents[KAFKA_110]["capabilities"]["max_safe_inflight"] = 2
    writer_c = await pool.acquire("history_kafka_c", snapshot)
    physical = pool._entries["history_kafka"].physical  # noqa: SLF001
    assert physical.limit == physical.quota.limit == 1
    assert physical.quota.inflight == 2

    waiting = asyncio.create_task(writer_c.write_batch(("c",)))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    assert fake_kafka.instances[-1].writes == 0

    gate.set()
    await asyncio.gather(first, second, waiting)
    assert fake_kafka.instances[-1].writes == 1

    await pool.release("history_kafka_b")
    assert physical.limit == 2
    await pool.release("history_kafka_c")
    assert physical.limit == 4
    await pool.release("history_kafka")
    await pool.release("history_kafka")


async def test_distinct_physical_targets_have_independent_quotas(
    fake_kafka: type[FakeKafkaAdapter],
) -> None:
    snapshot = _snapshot()
    snapshot.sinks["history_kafka_b"] = copy.deepcopy(snapshot.sinks["history_kafka"])
    snapshot.sinks["history_kafka_b"]["connection"]["bootstrap_servers"] = ["192.168.1.21:9092"]
    pool = SinkOutputPool()
    await pool.acquire("history_kafka", snapshot)
    await pool.acquire("history_kafka_b", snapshot)
    try:
        entry_a = pool._entries["history_kafka"]  # noqa: SLF001
        entry_b = pool._entries["history_kafka_b"]  # noqa: SLF001
        assert entry_a.physical is not entry_b.physical
        assert len(pool._physical_quotas) == 2  # noqa: SLF001
    finally:
        await pool.release("history_kafka")
        await pool.release("history_kafka_b")


async def test_legacy_template_forces_serial_quota(fake_kafka: type[FakeKafkaAdapter]) -> None:
    snapshot = _snapshot(version="1.0.0", concurrent=False, inflight=1, per_sink=8)
    pool = SinkOutputPool()
    await pool.acquire("history_kafka", snapshot)
    try:
        entry = pool._entries["history_kafka"]  # noqa: SLF001
        assert entry.quota.limit - entry.quota.inflight == 1  # noqa: SLF001
    finally:
        await pool.release("history_kafka")


async def test_distinct_writers_overlap_while_reentry_is_rejected(
    fake_kafka: type[FakeKafkaAdapter],
) -> None:
    snapshot = _snapshot()
    pool = SinkOutputPool()
    writer_a = await pool.acquire("history_kafka", snapshot)
    writer_b = await pool.acquire("history_kafka", snapshot)
    adapter = fake_kafka.instances[0]
    gate = adapter.block_next_write()
    try:
        first = asyncio.create_task(writer_a.write_batch(("s1",)))
        # 等待首个写入进入门控区。
        while adapter.inflight == 0:
            await asyncio.sleep(0.005)
        second = asyncio.create_task(writer_b.write_batch(("s2",)))
        for _ in range(20):
            await asyncio.sleep(0.005)
            if adapter.max_inflight == 2:
                break
        assert adapter.max_inflight == 2  # 不同设备（不同 Writer）真正并行
        # 同一 Writer 在途时重入必须被拒绝，不能占用第二份额度。
        reentry = await writer_a.write_batch(("s3",))
        assert reentry.delivered == ()
        assert "previous Sink I/O remains unsettled" in reentry.error
        gate.set()
        await asyncio.gather(first, second)
    finally:
        await pool.release("history_kafka")
        await pool.release("history_kafka")
    assert adapter.writes == 2


async def test_global_quota_serializes_when_set_to_one(
    fake_kafka: type[FakeKafkaAdapter],
) -> None:
    # 服务校验要求 per_sink <= global；两层同时收紧到 1 时写入必须串行。
    snapshot = _snapshot(global_quota=1, per_sink=1)
    pool = SinkOutputPool()
    writer_a = await pool.acquire("history_kafka", snapshot)
    writer_b = await pool.acquire("history_kafka", snapshot)
    adapter = fake_kafka.instances[0]
    gate = adapter.block_next_write()
    try:
        first = asyncio.create_task(writer_a.write_batch(("s1",)))
        while adapter.inflight == 0:
            await asyncio.sleep(0.005)
        second = asyncio.create_task(writer_b.write_batch(("s2",)))
        await asyncio.sleep(0.05)
        assert adapter.max_inflight == 1  # 全局额度 1：第二个写入在信号量外等待
        gate.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert adapter.writes == 2
        assert first_result.quota_stage_seconds is not None
        assert second_result.quota_stage_seconds is not None
        assert second_result.quota_stage_seconds[0] >= 0.03
        assert all(value >= 0 for value in second_result.quota_stage_seconds)
    finally:
        await pool.release("history_kafka")
        await pool.release("history_kafka")


async def test_quota_released_after_uncertain_timeout_and_settlement(
    fake_kafka: type[FakeKafkaAdapter],
) -> None:
    # io_wait_timeout_ms=1：门控写入观察超时返回 UNKNOWN，但额度保留至收敛。
    snapshot = _snapshot()
    snapshot.service["runtime"]["io_wait_timeout_ms"] = 1
    pool = SinkOutputPool()
    writer = await pool.acquire("history_kafka", snapshot)
    adapter = fake_kafka.instances[0]
    gate = adapter.block_next_write()
    try:
        outcome = await writer.write_batch(("s1",))
        assert outcome.delivered == ()
        assert "uncertain" in outcome.error
        entry = pool._entries["history_kafka"]  # noqa: SLF001
        # UNKNOWN 未收敛期间 Sink 额度仍被占用。
        assert entry.quota.limit - entry.quota.inflight == 3  # noqa: SLF001  容量 4，占 1
        gate.set()
        settled = await writer.settle()
        assert settled is not None and settled.delivered == (0,)
        assert settled.quota_stage_seconds is not None
        assert all(value >= 0 for value in settled.quota_stage_seconds)
        # 收敛回调释放额度。
        assert entry.quota.limit - entry.quota.inflight == 4  # noqa: SLF001
    finally:
        await pool.release("history_kafka")


# ---- B1 预编码接口必须复用同一套额度/UNKNOWN 所有权 ----------------------


async def test_encoded_batch_keeps_quota_through_uncertain_timeout(
    fake_kafka: type[FakeKafkaAdapter],
) -> None:
    snapshot = _snapshot()
    snapshot.service["runtime"]["io_wait_timeout_ms"] = 1
    pool = SinkOutputPool()
    writer = await pool.acquire("history_kafka", snapshot)
    adapter = fake_kafka.instances[0]
    gate = adapter.block_next_write()
    try:
        outcome = await writer.write_encoded_batch(
            (b'{"sample_id":"s1"}',), key=b"farm-a:collector-1:WT001", stripe=7
        )
        assert outcome.delivered == ()
        assert "uncertain" in outcome.error
        assert adapter.encoded_writes == 1
        assert adapter.encoded_last[1] == b"farm-a:collector-1:WT001"
        assert adapter.encoded_last[2] == 7
        entry = pool._entries["history_kafka"]  # noqa: SLF001
        assert entry.quota.limit - entry.quota.inflight == 3  # noqa: SLF001  UNKNOWN 期间额度不释放
        gate.set()
        settled = await writer.settle()
        assert settled is not None and settled.delivered == (0,)
        assert settled.quota_stage_seconds is not None
        assert settled.quota_stage_seconds[2] > 0
        assert entry.quota.limit - entry.quota.inflight == 4  # noqa: SLF001  终局回调释放
    finally:
        await pool.release("history_kafka")


async def test_encoded_batch_caller_cancel_does_not_leak_quota(
    fake_kafka: type[FakeKafkaAdapter],
) -> None:
    # shield 语义：调用方观察被取消时，底层 I/O 任务继续跑到终局，
    # done_callback 必须释放两层额度，不得留下孤儿任务或额度泄漏。
    snapshot = _snapshot()
    pool = SinkOutputPool()
    writer = await pool.acquire("history_kafka", snapshot)
    adapter = fake_kafka.instances[0]
    gate = adapter.block_next_write()
    entry = pool._entries["history_kafka"]  # noqa: SLF001
    try:
        cancelled = asyncio.create_task(
            writer.write_encoded_batch(
                (b'{"sample_id":"s1"}',), key=b"farm-a:collector-1:WT001", stripe=7
            )
        )
        while adapter.inflight == 0:
            await asyncio.sleep(0.005)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert entry.quota.limit - entry.quota.inflight == 3  # noqa: SLF001  底层任务仍持有额度
        gate.set()
        settled = await writer.settle()
        assert settled is not None and settled.delivered == (0,)
        assert entry.quota.limit - entry.quota.inflight == 4  # noqa: SLF001
        assert writer._pending is not None and writer._pending.done()  # noqa: SLF001
    finally:
        await pool.release("history_kafka")
