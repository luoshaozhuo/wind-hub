"""Collector acquisition state and device isolation tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from collector.application.acquisition import (
    AcquisitionPlan,
    CollectorAcquisition,
    DevicePlan,
    DeviceWorker,
)
from core.application import (
    ConnectionHealth,
    InterrogationRequest,
    ProtocolCapability,
    ProtocolPort,
    ProtocolSample,
)
from core.configuration import (
    SharedConfigPaths,
    SharedDocumentLoader,
    build_shared_snapshot,
)
from core.domain import Device, DeviceId, PointTable

pytestmark = [pytest.mark.component, pytest.mark.mock_service]
CONFIG = Path(__file__).resolve().parents[3] / "configs"


def _device() -> tuple[Device, PointTable]:
    shared = SharedDocumentLoader(
        SharedConfigPaths(
            CONFIG / "instances",
            CONFIG / "templates",
            site_file=CONFIG / "instances" / "site.yaml",
            devices_file=CONFIG / "instances" / "devices.yaml",
        )
    ).load()
    snapshot = build_shared_snapshot(shared)
    device = next(iter(snapshot.site.devices.values()))
    model = snapshot.device_models[device.device_model_id]
    return device, snapshot.point_tables[model.point_table_id]


class FakeDriver:
    def __init__(self) -> None:
        self.open = False
        self.connects = 0
        self.reads = 0
        self.unhealthy = False
        self.slow_read: asyncio.Event | None = None

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset({ProtocolCapability.READ_MANY})

    async def connect(self) -> None:
        self.connects += 1
        self.open = True

    async def close(self) -> None:
        self.open = False

    def is_open(self) -> bool:
        return self.open

    async def health(self) -> ConnectionHealth:
        return ConnectionHealth(not self.unhealthy)

    async def read_many(self, point_ids: tuple[str, ...]) -> tuple[ProtocolSample, ...]:
        self.reads += 1
        if self.slow_read is not None:
            await self.slow_read.wait()
        return tuple(
            ProtocolSample(point_id=point, value=1, timestamp=datetime.now(UTC))
            for point in point_ids
        )


class FakePublisher:
    def __init__(self) -> None:
        self.received: list[tuple[str, tuple[ProtocolSample, ...]]] = []

    def update_site(self, site_id: str) -> None:
        self.site_id = site_id

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def publish(self, device_id: str, samples: tuple[ProtocolSample, ...]) -> None:
        self.received.append((device_id, samples))


@pytest.mark.asyncio
async def test_poll_starts_and_stops_with_owned_session() -> None:
    device, table = _device()
    driver = FakeDriver()
    publisher = FakePublisher()
    worker = DeviceWorker(
        DevicePlan(device, table, 0.02, "START", "poll"),
        cast(ProtocolPort, driver),
        publisher,
        timeout=0.2,
        connect_timeout=0.2,
        watchdog_interval=0.02,
    )
    await worker.start()
    try:
        await asyncio.sleep(0.12)
        assert driver.connects == 1
        assert driver.reads >= 1
        assert publisher.received
        assert worker.actual_state == "RUNNING"
    finally:
        await worker.stop()
    assert not driver.open
    assert worker.actual_state == "STOPPED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("needs_reconnect", "state"),
    [(True, "RECOVERING"), (False, "STOPPING"), (True, "ISOLATED")],
)
async def test_stale_callback_during_recovery_never_promotes_running(
    needs_reconnect: bool, state: str
) -> None:
    device, table = _device()
    publisher = FakePublisher()
    worker = DeviceWorker(
        DevicePlan(device, table, 0.02, "START", "poll"),
        cast(ProtocolPort, FakeDriver()),
        publisher,
        timeout=0.2,
        connect_timeout=0.2,
        watchdog_interval=0.02,
    )
    point_id = next(
        point.point_id
        for point in table.points.values()
        if point.access.value in {"read", "read_write"}
    )
    samples = (ProtocolSample(point_id=point_id, value=1, timestamp=datetime.now(UTC)),)
    worker._needs_reconnect = needs_reconnect
    worker.actual_state = state
    await worker._on_samples(samples)
    assert worker.actual_state == state
    assert not publisher.received


@pytest.mark.asyncio
async def test_transient_isolation_with_healthy_io_still_promotes_running() -> None:
    device, table = _device()
    publisher = FakePublisher()
    worker = DeviceWorker(
        DevicePlan(device, table, 0.02, "START", "poll"),
        cast(ProtocolPort, FakeDriver()),
        publisher,
        timeout=0.2,
        connect_timeout=0.2,
        watchdog_interval=0.02,
    )
    point_id = next(
        point.point_id
        for point in table.points.values()
        if point.access.value in {"read", "read_write"}
    )
    samples = (ProtocolSample(point_id=point_id, value=1, timestamp=datetime.now(UTC)),)
    # The supervisor briefly reports ISOLATED while the owned poll read is
    # in flight; that read's own good samples must still advance the state.
    worker._needs_reconnect = False
    worker.actual_state = "ISOLATED"
    await worker._on_samples(samples)
    assert worker.actual_state == "RUNNING"
    assert publisher.received


@pytest.mark.asyncio
async def test_stop_intent_does_not_start_protocol_session() -> None:
    device, table = _device()
    driver = FakeDriver()
    worker = DeviceWorker(
        DevicePlan(device, table, 0.02, "STOP", "poll"),
        cast(ProtocolPort, driver),
        FakePublisher(),
        timeout=0.2,
        connect_timeout=0.2,
        watchdog_interval=0.02,
    )
    await worker.start()
    await asyncio.sleep(0.06)
    await worker.stop()
    assert driver.connects == 0


@pytest.mark.asyncio
async def test_unsettled_read_is_not_overlapped_by_reconnect() -> None:
    device, table = _device()
    driver = FakeDriver()
    release = asyncio.Event()
    driver.slow_read = release
    worker = DeviceWorker(
        DevicePlan(device, table, 0.02, "START", "poll"),
        cast(ProtocolPort, driver),
        FakePublisher(),
        timeout=0.03,
        connect_timeout=0.2,
        watchdog_interval=0.02,
    )
    await worker.start()
    try:
        await asyncio.sleep(0.12)
        assert driver.connects == 1
        assert driver.reads == 1
        assert worker.actual_state in {"ISOLATED", "FAULTED"}
    finally:
        release.set()
        await worker.stop()


@pytest.mark.asyncio
async def test_device_reconfigure_replaces_only_changed_worker() -> None:
    device, table = _device()
    drivers: list[FakeDriver] = []

    def factory(_device: Device, _table: PointTable) -> ProtocolPort:
        driver = FakeDriver()
        drivers.append(driver)
        return cast(ProtocolPort, driver)

    runtime = CollectorAcquisition(factory, FakePublisher())
    plan = AcquisitionPlan("site", (DevicePlan(device, table, 0.1, "STOP", "poll"),), 1, 1, 0.02)
    await runtime.start(plan)
    try:
        key = str(device.device_id)
        before = runtime.workers[key]
        await runtime.reconfigure(plan)
        assert runtime.workers[key] is before
        changed = AcquisitionPlan(
            "site", (DevicePlan(device, table, 0.1, "START", "poll"),), 1, 1, 0.02
        )
        await runtime.reconfigure(changed)
        assert runtime.workers[key] is not before
        assert len(drivers) == 2
    finally:
        await runtime.stop()


class FakeSubscription:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class FakeListener:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeSubscribeDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.subscription = FakeSubscription()

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset({ProtocolCapability.SUBSCRIBE})

    def subscribe(self, *args: object, **kwargs: object) -> FakeSubscription:
        return self.subscription


class FakeListenDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.listener = FakeListener()

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset({ProtocolCapability.LISTEN})

    async def listen(self, *args: object, **kwargs: object) -> FakeListener:
        return self.listener


@pytest.mark.asyncio
async def test_subscribe_mode_owns_remote_notification_handle() -> None:
    device, table = _device()
    driver = FakeSubscribeDriver()
    worker = DeviceWorker(
        DevicePlan(device, table, 0.02, "START", "subscribe"),
        cast(ProtocolPort, driver),
        FakePublisher(),
        timeout=0.2,
        connect_timeout=0.2,
        watchdog_interval=0.04,
    )
    await worker.start()
    try:
        await asyncio.sleep(0.10)
        assert driver.subscription.started
    finally:
        await worker.stop()
    assert driver.subscription.stopped
    assert driver.reads == 0


@pytest.mark.asyncio
async def test_listen_mode_owns_local_listener_handle() -> None:
    device, table = _device()
    driver = FakeListenDriver()
    worker = DeviceWorker(
        DevicePlan(device, table, 0.02, "START", "listen"),
        cast(ProtocolPort, driver),
        FakePublisher(),
        timeout=0.2,
        connect_timeout=0.2,
        watchdog_interval=0.04,
    )
    await worker.start()
    try:
        await asyncio.sleep(0.10)
        assert worker.actual_state == "STARTING"
    finally:
        await worker.stop()
    assert driver.listener.closed
    assert driver.reads == 0


class FakeReadOneDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.read_one_count = 0

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset({ProtocolCapability.READ})

    async def read_one(self, point_id: str) -> ProtocolSample:
        self.read_one_count += 1
        return ProtocolSample(point_id=point_id, value=3, timestamp=datetime.now(UTC))


class FakeInterrogationDriver(FakeDriver):
    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset({ProtocolCapability.INTERROGATE})

    async def interrogate(self, request: InterrogationRequest) -> tuple[ProtocolSample, ...]:
        assert request.kind == "station"
        return (ProtocolSample(point_id="active_power", value=3, timestamp=datetime.now(UTC)),)


@pytest.mark.asyncio
async def test_active_read_falls_back_to_serial_read_one() -> None:
    device, table = _device()
    driver = FakeReadOneDriver()
    driver.open = True
    worker = DeviceWorker(
        DevicePlan(device, table, 1.0, "STOP", "poll"),
        cast(ProtocolPort, driver),
        FakePublisher(),
        timeout=0.2,
        connect_timeout=0.2,
        watchdog_interval=1,
    )
    ids = tuple(
        point.point_id
        for point in table.points.values()
        if point.access.value in {"read", "read_write"}
    )
    received = await worker.read(ids)
    assert tuple(item.point_id for item in received) == ids
    assert driver.read_one_count == len(ids)
    assert driver.reads == 0


@pytest.mark.asyncio
async def test_interrogation_requires_protocol_capability() -> None:
    device, table = _device()
    unsupported = FakeDriver()
    unsupported.open = True
    worker = DeviceWorker(
        DevicePlan(device, table, 1.0, "STOP", "poll"),
        cast(ProtocolPort, unsupported),
        FakePublisher(),
        timeout=0.2,
        connect_timeout=0.2,
        watchdog_interval=1,
    )
    with pytest.raises(ValueError, match="interrogation not supported"):
        await worker.interrogate(InterrogationRequest())


class FakeBlockedHealthDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.release_probe = asyncio.Event()
        self.probes = 0

    async def health(self) -> ConnectionHealth:
        self.probes += 1
        if self.probes == 1:
            await self.release_probe.wait()
            return ConnectionHealth(False)
        return ConnectionHealth(True)


@pytest.mark.asyncio
async def test_health_timeout_never_reopens_unsettled_session() -> None:
    device, table = _device()
    driver = FakeBlockedHealthDriver()
    worker = DeviceWorker(
        DevicePlan(device, table, 0.02, "START", "poll"),
        cast(ProtocolPort, driver),
        FakePublisher(),
        timeout=0.03,
        connect_timeout=0.1,
        watchdog_interval=0.02,
    )
    await worker.start()
    try:
        await asyncio.sleep(0.16)
        assert driver.connects == 1
        assert worker.actual_state == "ISOLATED"
        driver.release_probe.set()
        await asyncio.sleep(0.12)
        assert driver.connects >= 2
    finally:
        driver.release_probe.set()
        await worker.stop()


@pytest.mark.asyncio
async def test_one_device_factory_failure_does_not_disrupt_other_devices() -> None:
    device, table = _device()
    second = replace(device, device_id=DeviceId("broken"))

    def factory(candidate: Device, _table: PointTable) -> ProtocolPort:
        if candidate.device_id == "broken":
            raise ValueError("driver options invalid")
        return cast(ProtocolPort, FakeDriver())

    runtime = CollectorAcquisition(factory, FakePublisher())
    plan = AcquisitionPlan(
        "site",
        (
            DevicePlan(device, table, 0.1, "STOP", "poll"),
            DevicePlan(second, table, 0.1, "STOP", "poll"),
        ),
        0.5,
        0.5,
        0.05,
    )
    await runtime.start(plan)
    try:
        assert str(device.device_id) in runtime.workers
        assert runtime.errors == {"broken": "driver options invalid"}
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_blocked_device_does_not_block_other_incremental_updates() -> None:
    device, table = _device()
    second = replace(device, device_id=DeviceId("second"))
    gate = asyncio.Event()
    created: dict[str, list[FakeDriver]] = {}

    def factory(candidate: Device, _table: PointTable) -> ProtocolPort:
        driver = FakeDriver()
        if candidate.device_id == device.device_id and not created.get(str(candidate.device_id)):
            driver.slow_read = gate
        created.setdefault(str(candidate.device_id), []).append(driver)
        return cast(ProtocolPort, driver)

    runtime = CollectorAcquisition(factory, FakePublisher())
    first = AcquisitionPlan(
        "site",
        (
            DevicePlan(device, table, 0.02, "START", "poll"),
            DevicePlan(second, table, 0.02, "START", "poll"),
        ),
        read_timeout=0.03,
        connect_timeout=0.2,
        watchdog_interval=0.02,
        shutdown_timeout=0.04,
    )
    await runtime.start(first)
    try:
        await asyncio.sleep(0.12)
        changed = AcquisitionPlan(
            "site",
            (
                DevicePlan(device, table, 0.03, "START", "poll"),
                DevicePlan(second, table, 0.03, "START", "poll"),
            ),
            0.03,
            0.2,
            0.02,
            0.04,
        )
        failures = await runtime.reconfigure(changed)
        assert str(device.device_id) in failures
        assert len(created["second"]) == 2
        gate.set()
        await asyncio.sleep(0.04)
        failures = await runtime.reconfigure(changed)
        assert failures == {}
    finally:
        gate.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_timed_out_active_rpc_keeps_device_session_owned() -> None:
    device, table = _device()
    driver = FakeDriver()
    gate = asyncio.Event()
    driver.slow_read = gate
    driver.open = True
    worker = DeviceWorker(
        DevicePlan(device, table, 0.1, "STOP", "poll"),
        cast(ProtocolPort, driver),
        FakePublisher(),
        timeout=2.0,
        connect_timeout=0.1,
        watchdog_interval=0.1,
    )
    point_ids = tuple(table.points)
    task = worker.submit_read(point_ids)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.02)
    assert not task.done()
    assert task in worker._operations
    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0.02)
    assert not stopping.done()
    assert driver.open
    gate.set()
    await stopping
    assert not driver.open
    assert task.done()


@pytest.mark.asyncio
async def test_failed_device_close_preserves_old_worker_for_retry() -> None:
    device, table = _device()

    # One transient close failure across the whole factory: the replacement
    # worker's later close (and process stop) must succeed.
    failure_budget = {"remaining": 1}

    class CloseFailsOnceDriver(FakeDriver):
        def __init__(self) -> None:
            super().__init__()
            self.closes = 0

        async def close(self) -> None:
            self.closes += 1
            if failure_budget["remaining"]:
                failure_budget["remaining"] -= 1
                raise RuntimeError("device close failed")
            await super().close()

    created: list[CloseFailsOnceDriver] = []

    def factory(_device: Device, _table: PointTable) -> ProtocolPort:
        driver = CloseFailsOnceDriver()
        created.append(driver)
        return cast(ProtocolPort, driver)

    acquisition = CollectorAcquisition(factory, FakePublisher())
    initial = AcquisitionPlan(
        "site",
        (DevicePlan(device, table, 1.0, "STOP", "poll"),),
        0.2,
        0.2,
        0.02,
        0.2,
    )
    await acquisition.start(initial, revision="revision-1")
    key = str(device.device_id)
    original = acquisition.workers[key]
    changed = AcquisitionPlan(
        "site",
        (DevicePlan(device, table, 0.5, "STOP", "poll"),),
        0.2,
        0.2,
        0.02,
        0.2,
    )
    try:
        failed = await acquisition.reconfigure(changed, revision="revision-2")
        assert key in failed
        assert acquisition.workers[key] is original
        assert original.applied_revision == "revision-1"
        assert len(created) == 1
        recovered = await acquisition.reconfigure(changed, revision="revision-2")
        assert recovered == {}
        assert acquisition.workers[key] is not original
        assert acquisition.workers[key].applied_revision == "revision-2"
        assert len(created) == 2
    finally:
        await acquisition.stop()
