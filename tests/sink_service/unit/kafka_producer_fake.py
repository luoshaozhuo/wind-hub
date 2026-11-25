"""Shared aiokafka BatchBuilder test double for encoded-delivery unit tests."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any

import pytest

from sink_service.infrastructure.adapters.outbound import realtime_adapters
from sink_service.infrastructure.adapters.outbound.realtime_adapters import KafkaOutput

TOPIC = "wind-hub-test"
KEY = b"farm-a:collector-1:WT001"
STRIPE = 7


class _FakeConfirmation:
    def __init__(self, *, fails: bool, gate: asyncio.Event | None = None) -> None:
        self.fails = fails
        self.gate = gate
        self.awaited = 0

    def __await__(self) -> Generator[Any, None, None]:
        self.awaited += 1
        if self.gate is not None:
            yield from self.gate.wait().__await__()
        if self.fails:
            raise RuntimeError("broker rejected confirmation")
        return None


class _FakeBuilder:
    def __init__(self, capacity: dict[str, int]) -> None:
        self.capacity = capacity
        self.records: list[tuple[bytes, bytes]] = []

    def append(self, *, key: bytes, value: bytes, timestamp: None) -> int | None:
        if len(self.records) >= self.capacity["value"]:
            return None
        self.records.append((key, value))
        return len(value)

    def close(self) -> None:
        return None


class _FakeCluster:
    def __init__(self) -> None:
        self.listeners: list[Any] = []

    def add_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener: Any) -> None:
        self.listeners.remove(listener)

    def fire_metadata_event(self) -> None:
        for listener in tuple(self.listeners):
            listener(self)


class _FakeClient:
    def __init__(self, producer: _FakeProducer) -> None:
        self.producer = producer
        self.cluster = _FakeCluster()

    async def _wait_on_metadata(self, _topic: str) -> None:
        self.producer.metadata_waits += 1


class _FakeProducer:
    def __init__(
        self,
        *,
        flags: dict[str, bool],
        gates: dict[str, Any],
        capacity: dict[str, int],
        **_options: Any,
    ) -> None:
        self.flags = flags
        self.gates = gates
        self.capacity = capacity
        self.client = _FakeClient(self)
        self.metadata_waits = 0
        self.partition_calls = 0
        self.send_attempts = 0
        self.submissions: list[tuple[_FakeBuilder, str, int]] = []
        self.futures: list[_FakeConfirmation] = []
        self.stopped = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True

    def _partition(self, *_args: Any) -> int:
        self.partition_calls += 1
        if self.flags["partition_fails"]:
            raise RuntimeError("partition unavailable")
        partitions: dict[bytes, int] = self.gates.get("partitions", {})
        return partitions.get(_args[2], 3)

    def create_batch(self) -> _FakeBuilder:
        return _FakeBuilder(self.capacity)

    async def send_batch(
        self, builder: _FakeBuilder, topic: str, *, partition: int
    ) -> _FakeConfirmation:
        callback = self.gates.get("on_send_batch")
        if callback is not None:
            callback()
        self.send_attempts += 1
        gate = self.gates.get("send_gate")
        if gate is not None:
            await gate.wait()
        if self.flags["send_batch_fails"]:
            raise RuntimeError("send_batch rejected")
        confirmation = _FakeConfirmation(
            fails=self.flags["confirm_fails"], gate=self.gates.get("confirm_gate")
        )
        self.futures.append(confirmation)
        self.submissions.append((builder, topic, partition))
        return confirmation


def _payloads(count: int) -> tuple[bytes, ...]:
    return tuple(f'{{"index":{index}}}'.encode() for index in range(count))


def _output() -> KafkaOutput:
    return KafkaOutput(servers=["127.0.0.1:9092"], topic=TOPIC, timeout_ms=10000)


def build_producer_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    flags = {"partition_fails": False, "send_batch_fails": False, "confirm_fails": False}
    gates: dict[str, Any] = {}
    capacity = {"value": 1000}

    def producer_class(**options: Any) -> _FakeProducer:
        return _FakeProducer(flags=flags, gates=gates, capacity=capacity, **options)

    producer = producer_class()

    def shared_producer_class(**_options: Any) -> _FakeProducer:
        return producer

    monkeypatch.setattr(
        realtime_adapters,
        "import_module",
        lambda name: SimpleNamespace(AIOKafkaProducer=shared_producer_class)
        if name == "aiokafka"
        else __import__(name),
    )
    producer = producer_class()
    return {
        "producer": producer,
        "producer_class": producer_class,
        "flags": flags,
        "gates": gates,
        "capacity": capacity,
    }
