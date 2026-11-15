"""Device-scoped acquisition runtime; protocol drivers are injected at assembly."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from core.application import (
    InterrogationRequest,
    ListenHandle,
    ProtocolCapability,
    ProtocolPort,
    ProtocolSample,
    Quality,
    SubscriptionHandle,
    validate_read_many_results,
)
from core.domain import Device, PointAccess, PointTable, engineering_value

from .ports.acquisition import MetricsSubjectPort, SampleSink
from .telemetry import CollectorTelemetry

T = TypeVar("T")

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DevicePlan:
    device: Device
    point_table: PointTable
    interval: float
    desired_state: str
    mode: str
    data_types: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AcquisitionPlan:
    site_id: str
    devices: tuple[DevicePlan, ...]
    read_timeout: float
    connect_timeout: float
    watchdog_interval: float
    shutdown_timeout: float = 10.0


DriverFactory = Callable[[Device, PointTable], ProtocolPort]


@dataclass(slots=True)
class DeviceWorker:
    plan: DevicePlan
    driver: ProtocolPort
    sink: SampleSink
    timeout: float
    connect_timeout: float
    watchdog_interval: float
    desired_state: str = "STOP"
    actual_state: str = "STOPPED"
    last_error: str = ""
    applied_revision: str = ""
    last_samples: tuple[ProtocolSample, ...] = ()
    telemetry: CollectorTelemetry | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _monitor: asyncio.Task[None] | None = None
    _poll: asyncio.Task[None] | None = None
    _subscription: SubscriptionHandle | None = None
    _listener: ListenHandle | None = None
    _inflight: asyncio.Future[Any] | None = None
    _operations: set[asyncio.Task[Any]] = field(default_factory=set)
    _closed: bool = False
    _wake: asyncio.Event = field(default_factory=asyncio.Event)
    _needs_reconnect: bool = False
    _recovery_started: float | None = None

    def __post_init__(self) -> None:
        self.desired_state = self.plan.desired_state

    async def _bounded(self, work: Awaitable[T], timeout: float) -> T:
        """Do not relinquish I/O ownership on a timeout."""
        if self._inflight is not None and not self._inflight.done():
            if inspect.iscoroutine(work):
                work.close()
            raise RuntimeError("device I/O is still unsettled")
        task = asyncio.ensure_future(work)
        self._inflight = task
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        finally:
            if task.done():
                self._inflight = None

    async def _settle(self) -> None:
        task = self._inflight
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done():
                    raise
            except Exception:
                pass
            finally:
                if task.done():
                    self._inflight = None

    async def start(self) -> None:
        if self._monitor is None:
            self._closed = False
            self._monitor = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        while not self._closed:
            try:
                if self.desired_state == "START":
                    if self._needs_reconnect:
                        await self._recover()
                    if self._inflight is not None and not self._inflight.done():
                        self.actual_state = "ISOLATED"
                    elif not self._needs_reconnect:
                        if self.driver.is_open() and not await self._check_health():
                            await self._recover()
                        if (
                            self._inflight is not None and not self._inflight.done()
                        ) or self._needs_reconnect:
                            self.actual_state = "ISOLATED"
                        else:
                            if not self.driver.is_open() and not self._closed:
                                async with self._lock:
                                    self.actual_state = "CONNECTING"
                                    await self._bounded(self.driver.connect(), self.connect_timeout)
                                    if self.telemetry is not None:
                                        self.telemetry.increment(
                                            "collector.device."
                                            f"{self.plan.device.device_id}.connections"
                                        )
                            if self._poll is not None and self._poll.done():
                                await self._recover()
                            if (
                                self.driver.is_open()
                                and not self._needs_reconnect
                                and self._poll is None
                                and self._subscription is None
                                and self._listener is None
                                and not self._closed
                            ):
                                async with self._lock:
                                    await self._start_collection()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.actual_state = "FAULTED"
                self.last_error = str(exc)
                if self.telemetry is not None:
                    self.telemetry.event(
                        "ERROR",
                        str(self.plan.device.device_id),
                        "DEVICE_RUNTIME_FAILED",
                        str(exc),
                    )
                if self._subscription is not None or self._listener is not None:
                    try:
                        await self._recover()
                    except Exception:
                        logger.exception("failed to release damaged acquisition handle")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.watchdog_interval)
            self._wake.clear()

    async def _recover(self) -> None:
        """Release the old acquisition handle before reconnecting."""
        self.actual_state = "RECOVERING"
        if self._recovery_started is None:
            self._recovery_started = time.monotonic()
        if self.telemetry is not None:
            self.telemetry.increment(
                f"collector.device.{self.plan.device.device_id}.disconnections"
            )
        self._needs_reconnect = True
        if any(not task.done() for task in self._operations) or (
            self._inflight is not None and not self._inflight.done()
        ):
            self.actual_state = "ISOLATED"
            return
        await self._release_handle()
        async with self._lock:
            if any(not task.done() for task in self._operations) or (
                self._inflight is not None and not self._inflight.done()
            ):
                self.actual_state = "ISOLATED"
                return
            await self.driver.close()
            self._needs_reconnect = False

    async def _release_handle(self) -> None:
        if self._poll is not None:
            self._poll.cancel()
            await asyncio.gather(self._poll, return_exceptions=True)
            self._poll = None
        # Keep ownership until each native handle confirms its close.
        # A failed close must not permit a replacement session.
        if self._subscription is not None:
            await self._subscription.stop()
            self._subscription = None
        if self._listener is not None:
            await self._listener.close()
            self._listener = None

    async def _check_health(self) -> bool:
        async with self._lock:
            if self._inflight is not None and not self._inflight.done():
                self.actual_state = "ISOLATED"
                return False
            if not self.driver.is_open():
                return False
            try:
                result = await self._bounded(self.driver.health(), self.timeout)
                if not result.healthy:
                    self.last_error = result.message or "device health check failed"
                return bool(result.healthy)
            except Exception as exc:
                self.last_error = str(exc)
                return False

    async def probe_health(self) -> bool:
        """Perform a serialized protocol health probe without reconnecting."""
        return await self._check_health()

    async def _start_collection(self) -> None:
        point_ids = tuple(
            point.point_id
            for point in self.plan.point_table.points.values()
            if point.access in (PointAccess.READ, PointAccess.READ_WRITE)
        )
        mode = self.plan.mode
        caps = self.driver.capabilities()
        if not point_ids:
            raise ValueError("device has no readable points")
        # Set STARTING before opening callback-based handles. A callback may
        # receive valid data during start() and advance the state to RUNNING.
        self.actual_state = "STARTING"
        if mode == "poll":
            if ProtocolCapability.READ_MANY not in caps:
                raise ValueError("driver does not support batch polling")
            self._poll = asyncio.create_task(self._poll_loop(point_ids))
        elif mode == "subscribe":
            if ProtocolCapability.SUBSCRIBE not in caps:
                raise ValueError("driver does not support native subscriptions")
            subscription = self.driver.subscribe(
                point_ids, self._on_samples, interval=self.plan.interval
            )
            self._subscription = subscription
            # Session establishment is protocol I/O too: an unbounded handshake
            # would pin this device lock and disable the watchdog permanently.
            await self._bounded(subscription.start(), self.connect_timeout)
        elif mode == "listen":
            if ProtocolCapability.LISTEN not in caps:
                raise ValueError("driver does not support native listening")
            self._listener = await self._bounded(
                self.driver.listen(point_ids, self._on_samples), self.connect_timeout
            )
        else:
            raise ValueError(f"unsupported acquisition mode: {mode}")
        self.last_error = ""

    async def _on_samples(self, samples: tuple[ProtocolSample, ...]) -> None:
        # ISOLATED is also set transiently while a healthy device has an
        # in-flight read, so it must not suppress the owned poll's own
        # samples; stale callbacks are excluded by _needs_reconnect and the
        # RECOVERING/STOPPING windows, whose handles have already been closed.
        if (
            self.desired_state != "START"
            or self._closed
            or self._needs_reconnect
            or self.actual_state in {"RECOVERING", "STOPPING"}
        ):
            return
        converted: list[ProtocolSample] = []
        for sample in samples:
            point = self.plan.point_table.points.get(sample.point_id)
            if point is None:
                continue
            try:
                value = engineering_value(point, sample.value)
                converted.append(replace(sample, value=value))
            except (ValueError, TypeError, OverflowError) as exc:
                self.last_error = f"{sample.point_id}: {exc}"
                converted.append(replace(sample, quality=Quality.BAD))
        if any(sample.quality is Quality.GOOD for sample in converted):
            if self._recovery_started is not None:
                if self.telemetry is not None:
                    self.telemetry.observe_milliseconds(
                        "collector.device." f"{self.plan.device.device_id}.recovery_ms",
                        (time.monotonic() - self._recovery_started) * 1000,
                    )
                    self.telemetry.increment(
                        f"collector.device.{self.plan.device.device_id}.reconnections"
                    )
                self._recovery_started = None
            self.actual_state = "RUNNING"
        if converted:
            if self.telemetry is not None:
                self.telemetry.increment("collector.points.collected", len(converted))
                bad = sum(item.quality is Quality.BAD for item in converted)
                self.telemetry.increment("collector.points.bad", bad)
                prefix = f"collector.device.{self.plan.device.device_id}."
                self.telemetry.increment(prefix + "points.collected", len(converted))
                self.telemetry.increment(prefix + "points.bad", bad)
            self.last_samples = tuple(converted)
            await self.sink.publish(str(self.plan.device.device_id), tuple(converted))

    async def _poll_loop(self, point_ids: tuple[str, ...]) -> None:
        next_slot = time.monotonic()
        while self.desired_state == "START" and not self._closed:
            next_slot += self.plan.interval
            started = time.monotonic()
            if self.telemetry is not None:
                self.telemetry.increment("collector.poll.executions")
                self.telemetry.observe_milliseconds(
                    "collector.poll.scheduler_deviation_ms",
                    max(0.0, (started - (next_slot - self.plan.interval)) * 1000),
                )
            try:
                async with self._lock:
                    received = await self._bounded(self.driver.read_many(point_ids), self.timeout)
                    samples = tuple(received)
                    validate_read_many_results(point_ids, samples)
                await self._on_samples(samples)
                if self.telemetry is not None:
                    self.telemetry.increment("collector.poll.success")
                    if any(item.quality is Quality.BAD for item in samples):
                        self.telemetry.increment("collector.poll.partial_success")
                    self.telemetry.observe_milliseconds(
                        "collector.poll.latency_ms",
                        (time.monotonic() - started) * 1000,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.actual_state = "FAULTED"
                self.last_error = str(exc)
                if self.telemetry is not None:
                    self.telemetry.increment("collector.poll.failed")
                    self.telemetry.event(
                        "ERROR",
                        str(self.plan.device.device_id),
                        "ACQUISITION_FAILED",
                        str(exc),
                    )
                return
            delay = next_slot - time.monotonic()
            if delay < 0:
                slots = int(-delay // self.plan.interval + 1)
                if self.telemetry is not None:
                    self.telemetry.increment("collector.poll.skipped_slots", slots)
                next_slot += slots * self.plan.interval
                delay = max(0.0, next_slot - time.monotonic())
            await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._closed = True
        self.desired_state = "STOP"
        self.actual_state = "STOPPING"
        if self._monitor is not None:
            self._wake.set()
            await self._monitor
            self._monitor = None
        if self._poll is not None:
            self._poll.cancel()
            await asyncio.gather(self._poll, return_exceptions=True)
            self._poll = None
        # Active RPC work and protocol I/O own the same session. Wait for
        # both before releasing it, without holding their acquisition lock.
        if self._operations:
            await asyncio.gather(*tuple(self._operations), return_exceptions=True)
        await self._settle()
        async with self._lock:
            if self._subscription is not None:
                await self._subscription.stop()
                self._subscription = None
            if self._listener is not None:
                await self._listener.close()
                self._listener = None
            await self.driver.close()
            self.actual_state = "STOPPED"

    def submit_health(self) -> asyncio.Task[bool]:
        """Own diagnostic health I/O beyond gRPC cancellation or timeout."""
        task = asyncio.create_task(self.probe_health())
        self._operations.add(task)
        task.add_done_callback(self._operations.discard)
        return task

    def submit_read(self, point_ids: tuple[str, ...]) -> asyncio.Task[tuple[ProtocolSample, ...]]:
        """Own active reads after the requesting RPC times out or disconnects."""
        task = asyncio.create_task(self.read(point_ids))
        self._operations.add(task)
        task.add_done_callback(self._operations.discard)
        return task

    def submit_interrogation(
        self, request: InterrogationRequest
    ) -> asyncio.Task[tuple[ProtocolSample, ...]]:
        task = asyncio.create_task(self.interrogate(request))
        self._operations.add(task)
        task.add_done_callback(self._operations.discard)
        return task

    async def read(self, point_ids: tuple[str, ...]) -> tuple[ProtocolSample, ...]:
        """One active read, sharing exclusive session access with background reads."""
        for point_id in point_ids:
            point = self.plan.point_table.points.get(point_id)
            if point is None or point.access not in (PointAccess.READ, PointAccess.READ_WRITE):
                raise ValueError(f"unknown or non-readable point: {point_id}")
        async with self._lock:
            if not self.driver.is_open():
                raise RuntimeError("device is not connected")
            capabilities = self.driver.capabilities()
            if ProtocolCapability.READ_MANY in capabilities:
                samples = tuple(await self._bounded(self.driver.read_many(point_ids), self.timeout))
            elif ProtocolCapability.READ in capabilities:
                deadline = time.monotonic() + self.timeout
                values: list[ProtocolSample] = []
                for point_id in point_ids:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("active read deadline expired")
                    values.append(await self._bounded(self.driver.read_one(point_id), remaining))
                samples = tuple(values)
            else:
                raise ValueError("device does not support active reading")
            expected = set(point_ids)
            received_ids = [sample.point_id for sample in samples]
            if len(received_ids) != len(set(received_ids)):
                raise ValueError("duplicate points returned by protocol")
            if not set(received_ids).issubset(expected):
                raise ValueError("protocol returned unrequested points")
            return samples

    async def interrogate(self, request: InterrogationRequest) -> tuple[ProtocolSample, ...]:
        async with self._lock:
            if not self.driver.is_open():
                raise RuntimeError("device is not connected")
            if ProtocolCapability.INTERROGATE not in self.driver.capabilities():
                raise ValueError("interrogation not supported")
            return tuple(await self._bounded(self.driver.interrogate(request), self.timeout))


class CollectorAcquisition:
    """Own each device worker and publisher; only changed devices are recreated."""

    def __init__(
        self,
        factory: DriverFactory,
        sink: SampleSink,
        *,
        telemetry: CollectorTelemetry | None = None,
        metrics_subject: MetricsSubjectPort | None = None,
    ) -> None:
        self._telemetry = telemetry
        self._metrics_subject = metrics_subject
        self._factory = factory
        self._sink = sink
        self._workers: dict[str, DeviceWorker] = {}
        self._errors: dict[str, str] = {}
        self._stopping: dict[str, asyncio.Task[None]] = {}
        self._shutdown_timeout = 10.0
        self._lock = asyncio.Lock()

    @property
    def workers(self) -> dict[str, DeviceWorker]:
        return dict(self._workers)

    @property
    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    async def start(self, plan: AcquisitionPlan, revision: str = "") -> None:
        await self._sink.start()
        try:
            errors = await self.reconfigure(plan, revision=revision)
            if errors and self._telemetry is not None:
                for key, message in errors.items():
                    self._telemetry.event("ERROR", key, "DEVICE_STARTUP_FAILED", message)
        except BaseException:
            await self.stop()
            raise

    async def reconfigure(self, plan: AcquisitionPlan, *, revision: str = "") -> dict[str, str]:
        """Apply independent device changes and retain failed objects for retry."""
        async with self._lock:
            self._shutdown_timeout = plan.shutdown_timeout
            desired = {str(item.device.device_id): item for item in plan.devices}
            errors: dict[str, str] = {}
            for key, worker in tuple(self._workers.items()):
                if desired.get(key) != worker.plan:
                    task = self._stopping.get(key)
                    if task is not None and task.done():
                        try:
                            task.result()
                        except Exception:
                            # Explicitly retry a failed close; preserve the
                            # original worker until it succeeds.
                            self._stopping.pop(key, None)
                            task = None
                    if task is None:
                        task = asyncio.create_task(worker.stop())
                        self._stopping[key] = task
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=self._shutdown_timeout)
                    except TimeoutError:
                        if self._telemetry is not None:
                            self._telemetry.event(
                                "ERROR",
                                key,
                                "UNSETTLED_IO",
                                "device stop exceeded configured deadline",
                            )
                        worker.actual_state = "ISOLATED"
                        errors[key] = "old device I/O has not safely settled"
                    except Exception as exc:
                        # A failed close does not grant permission to replace
                        # the worker: the old session still owns its resources.
                        errors[key] = str(exc) or type(exc).__name__
                    else:
                        self._stopping.pop(key, None)
                        del self._workers[key]
            for key, item in desired.items():
                if key in self._workers:
                    continue
                try:
                    worker = DeviceWorker(
                        item,
                        self._factory(item.device, item.point_table),
                        self._sink,
                        plan.read_timeout,
                        plan.connect_timeout,
                        plan.watchdog_interval,
                        telemetry=self._telemetry,
                    )
                    self._workers[key] = worker
                    await worker.start()
                    worker.applied_revision = revision
                except Exception as exc:
                    self._workers.pop(key, None)
                    errors[key] = str(exc) or type(exc).__name__
            if not errors:
                for worker in self._workers.values():
                    worker.applied_revision = revision
                self._sink.update_site(plan.site_id)
                if self._metrics_subject is not None:
                    self._metrics_subject.update_site(plan.site_id)
            else:
                # Unchanged healthy devices have applied the target
                # configuration even if a different device failed to settle.
                for key, worker in self._workers.items():
                    if key not in errors and desired.get(key) == worker.plan:
                        worker.applied_revision = revision
            self._errors = errors
            return dict(errors)

    async def stop(self) -> None:
        """Attempt every device stop concurrently, without abandoning blocked I/O."""
        async with self._lock:
            for key, worker in self._workers.items():
                if key not in self._stopping:
                    self._stopping[key] = asyncio.create_task(worker.stop())
            tasks = set(self._stopping.values())
            pending: set[asyncio.Task[None]] = set()
            failures: list[str] = []
            failed_keys: set[str] = set()
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=self._shutdown_timeout)
                for task in done:
                    try:
                        task.result()
                    except Exception as exc:
                        failures.append(str(exc) or type(exc).__name__)
                        failed_keys.update(
                            key for key, operation in self._stopping.items() if operation is task
                        )
            # Unsettled tasks remain owned and observable; never mark safe.
            self._stopping = {
                key: task
                for key, task in self._stopping.items()
                if task in pending or key in failed_keys
            }
            self._workers = {
                key: worker
                for key, worker in self._workers.items()
                if key in self._stopping or key in failed_keys
            }
            await self._sink.stop()
            if pending or failures:
                raise RuntimeError(
                    f"Collector stop left {len(pending)} unsettled I/O task(s): "
                    + "; ".join(failures)
                )
