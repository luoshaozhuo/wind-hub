"""Scheduler — per-device polling, pipeline execution, sink dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from wind_hub.config.schema import (
    DeviceConfig,
    PointConfig,
    PollingGroup,
    SchedulerConfig,
    SinkConfig,
)
from wind_hub.domain.engine.pipeline import Pipeline
from wind_hub.domain.engine.router import Router
from wind_hub.domain.model.point import PointRef, PointValue
from wind_hub.domain.port.outbound import HealthStatus, ProtocolPort, SinkPort

logger = logging.getLogger(__name__)


def _is_connection_level(exc: BaseException) -> bool:
    """判定异常是否属于「连接级」故障（对端不可达的日常表现）。

    沿 ``__cause__`` 链检查：设备驱动通常把底层 ``OSError`` /
    ``TimeoutError`` 包装成 ``ProtocolError`` 再抛出（如 IEC104 的
    TCP connect 失败），只看最外层类型会漏判，因此穿透包装链。
    ``ConnectionRefusedError`` 是 ``OSError`` 的子类，一并覆盖。
    """
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError | ConnectionRefusedError | OSError):
            return True
        current = current.__cause__
    return False


class Scheduler:
    """Acquisition scheduler — drives the collect → process → route → sink pipeline.

    Injected dependencies (all concrete implementations from adapters):
        *devices* — device configurations from config loader.
        *protocols* — ProtocolPort instances keyed by device_id.
        *pipeline* — processor chain.
        *router* — point-to-sink routing.
        *sinks* — SinkPort instances keyed by sink name.
        *config* — SchedulerConfig from system.yaml.
        *points_by_device* — point tables keyed by device_id, injected into
            each protocol driver before polling begins.  ``None`` (or a missing
            device key) means that device has no points, so nothing is read.
        *on_points_collected* — optional callback invoked with the number of
            point values entering the process → route pipeline, on both the
            polling and the subscription-push paths (决策 0.1).  The
            composition root wires this to the Prometheus
            ``points_collected_total`` counter; domain stays decoupled from
            infra (import-linter forbids ``domain → infra``).
    """

    def __init__(
        self,
        devices: dict[str, DeviceConfig],
        protocols: dict[str, ProtocolPort],
        pipeline: Pipeline,
        router: Router,
        sinks: dict[str, SinkPort],
        config: SchedulerConfig,
        points_by_device: dict[str, list[PointConfig]] | None = None,
        on_points_collected: Callable[[int], None] | None = None,
    ) -> None:
        self._devices = devices
        self._protocols = protocols
        self._pipeline = pipeline
        self._router = router
        self._sinks = sinks
        self._config = config
        self._points_by_device = points_by_device or {}
        self._on_points_collected = on_points_collected or (lambda _n: None)

        # Synchronous observers notified with the full processed batch after
        # each collection + routing pass.  Seeded by :meth:`add_observer`.
        self._observers: list[Callable[[list[PointValue]], None]] = []

        # Per-sink bounded queues
        self._queues: dict[str, asyncio.Queue[list[PointValue]]] = {
            name: asyncio.Queue(maxsize=config.queue_maxsize) for name in sinks
        }

        # Task bookkeeping
        self._device_tasks: dict[str, asyncio.Task[Any]] = {}
        self._sink_tasks: dict[str, asyncio.Task[Any]] = {}
        self._running = False
        # ``start()`` 完整完成（设备连接尝试结束、任务已拉起）才置位；
        # ``running`` 属性把它与 ``_running`` 取与，让 /health 能区分
        # 「启动进行中」（决策 1）与「已就绪」。
        self._started = False

        # Sinks whose ``open()`` raised during ``start()`` — they are skipped
        # and surfaced as unhealthy by :meth:`health`.
        self._unhealthy_sinks: set[str] = set()

        # 运行期统计（决策 7）：单调累计，经 SystemStatus 暴露给 /health。
        self._points_collected = 0
        self._points_routed = 0
        self._points_dropped = 0

    # ------------------------------------------------------------------
    # public lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the scheduler — connect devices, open sinks, launch loops.

        A single device failing to connect is logged and skipped;
        other devices continue normally.
        """
        if self._running:
            return
        self._running = True
        self._started = False

        # 0. Inject each device's point table into its protocol driver.  This is
        #    pure in-memory and must precede any read; a ConfigError (bad point
        #    table) fails fast here rather than mid-poll.
        for device_id, proto in self._protocols.items():
            proto.set_points_mapping(self._points_by_device.get(device_id, []))

        # 1. Connect all devices (best-effort, failures logged)
        for device_id, proto in self._protocols.items():
            try:
                await asyncio.wait_for(proto.connect(), timeout=self._config.connect_timeout)
                logger.info("Device '%s' connected", device_id)
            except Exception as exc:
                # 决策 0.3：连接级故障（超时/拒连/网络不可达，含驱动包装链
                # 里的底层 OSError）是现场日常，简洁 warning 不打堆栈；
                # 其他异常（编程错误、协议实现缺陷）保留完整堆栈以便排查。
                if _is_connection_level(exc):
                    logger.warning("Device '%s' failed to connect — skipped: %s", device_id, exc)
                else:
                    logger.warning(
                        "Device '%s' failed to connect — skipped", device_id, exc_info=True
                    )

        # 2. Open all sinks (best-effort — a single failing sink is skipped so
        #    the engine still starts; it is surfaced as unhealthy by health()).
        for name, sink in self._sinks.items():
            try:
                await sink.open()
                logger.info("Sink '%s' opened", name)
            except Exception:
                logger.warning("Sink '%s' failed to open — skipped", name, exc_info=True)
                self._unhealthy_sinks.add(name)

        # 3. Launch sink consumer tasks (only for sinks that opened)
        for name, sink in self._sinks.items():
            if name in self._unhealthy_sinks:
                continue
            task = asyncio.create_task(self._sink_consumer(name, sink))
            self._sink_tasks[name] = task

        # 4. Launch per-device acquisition (poll and/or subscribe by mode)
        for device_id, device_cfg in self._devices.items():
            await self._start_acquisition(device_id, device_cfg)

        # 全部启动步骤完成后才对外报告 running（决策 1：/health 可区分
        # 「启动进行中」——设备连接超时期间 running 保持 False）。
        self._started = True

    async def stop(self) -> None:
        """Graceful shutdown — cancel polls, drain queues, close connections.

        Args:
            timeout: Maximum seconds to wait for in-flight operations.
        """
        if not self._running:
            return
        self._running = False
        self._started = False

        # 1. Cancel all device polling tasks
        for task in self._device_tasks.values():
            task.cancel()
        if self._device_tasks:
            await asyncio.wait(
                list(self._device_tasks.values()),
                timeout=self._config.shutdown_timeout,
            )
            self._device_tasks.clear()

        # 2. Signal sink loops to finish by putting sentinel + cancel
        for queue in self._queues.values():
            await queue.put([])  # empty list = shutdown sentinel
        for task in self._sink_tasks.values():
            try:
                await asyncio.wait_for(task, timeout=self._config.shutdown_timeout)
            except TimeoutError:
                task.cancel()
            except asyncio.CancelledError:
                pass
        self._sink_tasks.clear()

        # 3. Flush and close sinks
        for name, sink in self._sinks.items():
            try:
                await sink.flush()
            except Exception:
                logger.warning("Sink '%s' flush failed", name, exc_info=True)
            try:
                await sink.close()
            except Exception:
                logger.warning("Sink '%s' close failed", name, exc_info=True)

        # 4. Close protocols
        for device_id, proto in self._protocols.items():
            try:
                await proto.close()
            except Exception:
                logger.warning("Device '%s' close failed", device_id, exc_info=True)

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------

    def health(self) -> dict[str, HealthStatus]:
        """Return health status for all devices and sinks."""
        result: dict[str, HealthStatus] = {}
        for device_id, proto in self._protocols.items():
            result[device_id] = proto.health()
        for name, sink in self._sinks.items():
            if name in self._unhealthy_sinks:
                result[name] = HealthStatus(healthy=False, message="open failed")
            else:
                result[name] = sink.health()
        return result

    @property
    def running(self) -> bool:
        """Whether the engine loop is fully started and active.

        ``True`` only after :meth:`start` has completed all its steps
        (device connect attempts, sink opens, task launch) and before
        :meth:`stop` begins.  During startup — e.g. while unreachable
        devices are still inside their ``connect_timeout`` — this is
        ``False`` even though acquisition tasks may already be coming up,
        which lets ``/health`` distinguish "starting" from "ready"
        (决策 1).  It is the authoritative "is the engine collecting"
        signal for :class:`TaskService.status` (which derives
        ``SystemStatus.running`` from it).
        """
        return self._running and self._started

    @property
    def device_count(self) -> int:
        return len(self._devices)

    @property
    def sink_count(self) -> int:
        return len(self._sinks)

    @property
    def points_collected(self) -> int:
        """累计采集点数——进入处理管线的点值总数，含轮询与订阅推送
        （单调不减，决策 7 / 0.1 口径统一）。"""
        return self._points_collected

    @property
    def points_routed(self) -> int:
        """累计路由点数——成功进入 sink 队列的点值总数（单调不减，决策 7）。"""
        return self._points_routed

    @property
    def points_dropped(self) -> int:
        """累计丢弃点数——背压策略丢弃的点值总数（单调不减，决策 7）。"""
        return self._points_dropped

    def add_observer(self, callback: Callable[[list[PointValue]], None]) -> None:
        """Register a synchronous observer of collected point values.

        Every ``callback`` is invoked once per collection pass (poll or
        push, after processing and before routing) with the full batch of
        :class:`PointValue` instances.  Observers run synchronously on the
        event loop — a raised exception is caught and logged so it can never
        disturb the collection pipeline.
        """
        self._observers.append(callback)

    @property
    def current_router(self) -> Router:
        """Return the *current* router, reflecting any hot-reload swap.

        ``RouteService`` and other router consumers hold the scheduler and read
        through this property so that a call to
        :meth:`replace_router` during hot-reload is observed immediately.
        """
        return self._router

    # ------------------------------------------------------------------
    # hot-reload — device management
    # ------------------------------------------------------------------

    async def add_device(
        self,
        device_id: str,
        cfg: DeviceConfig,
        protocol: ProtocolPort,
        points: list[PointConfig],
    ) -> None:
        """Add a new device at runtime — inject point table, connect, start polling."""
        self._devices[device_id] = cfg
        self._protocols[device_id] = protocol
        protocol.set_points_mapping(points)
        self._points_by_device[device_id] = points

        try:
            await asyncio.wait_for(protocol.connect(), timeout=self._config.connect_timeout)
            logger.info("Hot-reload: device '%s' connected", device_id)
        except Exception:
            logger.warning(
                "Hot-reload: device '%s' failed to connect — " "task launched anyway",
                device_id,
                exc_info=True,
            )

        if cfg.enabled and self._running:
            await self._start_acquisition(device_id, cfg)

    async def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime — stop task and close connection."""

        # Cancel and wait for device task
        task = self._device_tasks.pop(device_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=self._config.shutdown_timeout)

        # Close protocol
        proto = self._protocols.pop(device_id, None)
        if proto is not None:
            try:
                await proto.close()
            except Exception:
                logger.warning(
                    "Hot-reload: device '%s' close failed",
                    device_id,
                    exc_info=True,
                )

        self._devices.pop(device_id, None)
        logger.info("Hot-reload: device '%s' removed", device_id)

    async def rebuild_device(
        self,
        device_id: str,
        new_cfg: DeviceConfig,
        new_protocol: ProtocolPort,
        points: list[PointConfig],
    ) -> None:
        """Rebuild a device — stop old, swap config/protocol/points, start new."""
        # Stop old task and close old protocol
        task = self._device_tasks.pop(device_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=self._config.shutdown_timeout)

        old_proto = self._protocols.pop(device_id, None)
        if old_proto is not None:
            try:
                await old_proto.close()
            except Exception:
                logger.warning(
                    "Hot-reload: old protocol close failed for '%s'",
                    device_id,
                    exc_info=True,
                )

        # Swap in new config and protocol
        self._devices[device_id] = new_cfg
        self._protocols[device_id] = new_protocol
        new_protocol.set_points_mapping(points)
        self._points_by_device[device_id] = points

        # Connect new protocol
        try:
            await asyncio.wait_for(new_protocol.connect(), timeout=self._config.connect_timeout)
            logger.info("Hot-reload: device '%s' reconnected", device_id)
        except Exception:
            logger.warning(
                "Hot-reload: device '%s' connect failed after rebuild",
                device_id,
                exc_info=True,
            )

        # Start new task
        if new_cfg.enabled and self._running:
            await self._start_acquisition(device_id, new_cfg)

    # ------------------------------------------------------------------
    # hot-reload — sink management
    # ------------------------------------------------------------------

    async def add_sink(self, sink_name: str, cfg: SinkConfig, sink: SinkPort) -> None:
        """Add a new sink at runtime — open, create queue, start consumer."""
        self._sinks[sink_name] = sink
        queue: asyncio.Queue[list[PointValue]] = asyncio.Queue(maxsize=self._config.queue_maxsize)
        self._queues[sink_name] = queue

        await sink.open()
        logger.info("Hot-reload: sink '%s' opened", sink_name)

        if self._running:
            task = asyncio.create_task(self._sink_consumer(sink_name, sink))
            self._sink_tasks[sink_name] = task

    async def remove_sink(self, sink_name: str) -> None:
        """Remove a sink at runtime — stop consumer, drain, close."""

        # Stop consumer task
        task = self._sink_tasks.pop(sink_name, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=self._config.shutdown_timeout)

        # Flush and close
        old_sink = self._sinks.pop(sink_name, None)
        if old_sink is not None:
            try:
                await old_sink.flush()
            except Exception:
                logger.warning(
                    "Hot-reload: sink '%s' flush failed",
                    sink_name,
                    exc_info=True,
                )
            try:
                await old_sink.close()
            except Exception:
                logger.warning(
                    "Hot-reload: sink '%s' close failed",
                    sink_name,
                    exc_info=True,
                )

        self._queues.pop(sink_name, None)
        logger.info("Hot-reload: sink '%s' removed", sink_name)

    async def rebuild_sink(self, sink_name: str, new_cfg: SinkConfig, new_sink: SinkPort) -> None:
        """Rebuild a sink — stop old consumer, swap, start new consumer.

        The existing queue is preserved so no in-flight data is lost.
        """

        # Stop old consumer
        task = self._sink_tasks.pop(sink_name, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=self._config.shutdown_timeout)

        # Flush and close old sink
        old_sink = self._sinks.pop(sink_name, None)
        if old_sink is not None:
            try:
                await old_sink.flush()
            except Exception:
                logger.warning(
                    "Hot-reload: sink '%s' flush failed during rebuild",
                    sink_name,
                    exc_info=True,
                )
            try:
                await old_sink.close()
            except Exception:
                logger.warning(
                    "Hot-reload: sink '%s' close failed during rebuild",
                    sink_name,
                    exc_info=True,
                )

        # Swap in new sink (preserve existing queue)
        self._sinks[sink_name] = new_sink
        await new_sink.open()
        logger.info("Hot-reload: sink '%s' re-opened", sink_name)

        if self._running:
            new_task = asyncio.create_task(self._sink_consumer(sink_name, new_sink))
            self._sink_tasks[sink_name] = new_task

    # ------------------------------------------------------------------
    # hot-reload — router / pipeline swap
    # ------------------------------------------------------------------

    async def replace_router(self, new_router: Router) -> None:
        """Atomically swap the routing table.

        Called when points or routing rules change.  Sink queues are
        not affected.
        """
        self._router = new_router
        logger.info("Router replaced (%d entries)", new_router.table_size)

    async def replace_pipeline(self, new_pipeline: Pipeline) -> None:
        """Atomically swap the processor pipeline.

        Called when the processor list changes.
        """
        self._pipeline = new_pipeline
        logger.info("Pipeline replaced (%d processors)", new_pipeline.processor_count)

    # ------------------------------------------------------------------
    # private — acquisition (poll / subscribe)
    # ------------------------------------------------------------------

    async def _start_acquisition(self, device_id: str, device_cfg: DeviceConfig) -> None:
        """Start a device's acquisition task(s) according to its ``mode``.

        ``'poll'`` launches the polling loop; ``'subscribe'`` subscribes to
        spontaneous updates (no polling); ``'both'`` does both.  Disabled
        devices are skipped.
        """
        if not device_cfg.enabled:
            return
        if device_cfg.mode in ("poll", "both"):
            task = asyncio.create_task(self._device_loop(device_id, device_cfg))
            self._device_tasks[device_id] = task
        if device_cfg.mode in ("subscribe", "both"):
            await self._start_subscription(device_id, device_cfg)

    async def _start_subscription(self, device_id: str, device_cfg: DeviceConfig) -> None:
        """Subscribe a device to spontaneous updates (push mode).

        The driver's ``subscribe`` callback feeds each pushed value back into
        the normal process → route → sink pipeline.  If the driver does not
        support subscription (``NotImplementedError``) or the call fails, a
        pure-``'subscribe'`` device falls back to polling so it still collects.
        """
        proto = self._protocols.get(device_id)
        if proto is None:
            return
        refs = [
            PointRef(device_id=device_id, point_id=p.point_id)
            for p in self._points_by_device.get(device_id, [])
        ]

        async def on_data(value: PointValue) -> None:
            await self._process_and_route([value])

        try:
            await proto.subscribe(refs, on_data)
            logger.info("Device '%s' subscribed to updates", device_id)
        except NotImplementedError:
            logger.warning(
                "Device '%s' does not support subscription — falling back to polling",
                device_id,
            )
            self._fallback_to_polling(device_id, device_cfg)
        except Exception:
            logger.warning(
                "Device '%s' subscribe failed — falling back to polling",
                device_id,
                exc_info=True,
            )
            self._fallback_to_polling(device_id, device_cfg)

    def _fallback_to_polling(self, device_id: str, device_cfg: DeviceConfig) -> None:
        """Switch a subscribe-only device to polling after subscribe fails."""
        if device_cfg.mode == "subscribe":
            task = asyncio.create_task(self._device_loop(device_id, device_cfg))
            self._device_tasks[device_id] = task

    # ------------------------------------------------------------------
    # private — device loop
    # ------------------------------------------------------------------

    async def _device_loop(self, device_id: str, device_cfg: DeviceConfig) -> None:
        """Per-device polling loop — one asyncio.Task per device."""
        proto = self._protocols.get(device_id)
        if proto is None:
            logger.warning("Device '%s' has no protocol — skipping loop", device_id)
            return

        # Build polling groups: default group if none configured
        groups: list[PollingGroup] = list(device_cfg.polling)
        if not groups:
            groups = [
                PollingGroup(
                    group="default",
                    interval=self._config.default_interval,
                )
            ]

        try:
            while self._running:
                # Concurrently poll each group
                results: list[list[PointValue] | BaseException] = await asyncio.gather(
                    *[self._poll_group_once(device_id, g) for g in groups],
                    return_exceptions=True,
                )
                for res in results:
                    if isinstance(res, BaseException):
                        logger.warning(
                            "Polling group failed for device '%s': %s",
                            device_id,
                            res,
                        )
                        continue
                    await self._process_and_route(res)

                # Wait one interval of the fastest group
                min_interval = min(g.interval for g in groups)
                await asyncio.sleep(min_interval)
        except asyncio.CancelledError:
            logger.info("Device loop '%s' cancelled", device_id)
            raise

    async def _poll_group_once(self, device_id: str, group: PollingGroup) -> list[PointValue]:
        """Poll a single group once — read the device's points.

        ``PointConfig`` carries no group tag yet, so group filtering is not
        possible; we read the device's full point set on every group tick.
        """
        proto = self._protocols[device_id]
        refs = [
            PointRef(device_id=device_id, point_id=p.point_id)
            for p in self._points_by_device.get(device_id, [])
        ]
        return await proto.read(refs)

    async def _process_and_route(self, batch: list[PointValue]) -> None:
        """Process, route, and push a batch to sink queues.

        采集统计在**本方法入口**统一计数（决策 0.1）：轮询（``_device_loop``
        调用）与订阅推送（``on_data`` 回调调用）两条路径都经过这里，因此
        ``points_collected`` 属性与注入的 ``on_points_collected`` 回调
        （Prometheus ``points_collected_total``）口径一致、都含订阅推送。
        """
        if not batch:
            return
        self._points_collected += len(batch)
        self._on_points_collected(len(batch))
        processed = await self._pipeline.process(batch)
        self._notify_observers(processed)
        routed = self._router.route(processed)
        await self._push_to_sinks(routed)

    def _notify_observers(self, values: list[PointValue]) -> None:
        """Invoke every observer with *values*, isolating their exceptions."""
        for observer in self._observers:
            try:
                observer(values)
            except Exception:
                logger.warning("Observer raised during collection — ignored", exc_info=True)

    # ------------------------------------------------------------------
    # private — sink dispatch
    # ------------------------------------------------------------------

    async def _push_to_sinks(self, routed: dict[str, list[PointValue]]) -> None:
        """Push routed batches into each sink's queue, respecting backpressure."""
        for sink_name, batch in routed.items():
            if not batch:
                continue
            queue = self._queues.get(sink_name)
            if queue is None:
                continue
            await self._handle_backpressure(queue, batch, sink_name)

    async def _handle_backpressure(
        self,
        queue: asyncio.Queue[list[PointValue]],
        batch: list[PointValue],
        sink_name: str,
    ) -> None:
        """Enqueue a batch using the configured backpressure policy."""
        policy = self._config.backpressure_policy

        if policy == "drop_new":
            if queue.full():
                self._points_dropped += len(batch)
                logger.warning(
                    "Sink '%s' queue full (%d) — dropping new batch (%d points)",
                    sink_name,
                    queue.maxsize,
                    len(batch),
                )
                return
            await queue.put(batch)
            self._points_routed += len(batch)

        elif policy == "drop_old":
            # Drain oldest entries until there is room
            while queue.full():
                try:
                    evicted = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._points_dropped += len(evicted)
            await queue.put(batch)
            self._points_routed += len(batch)

        elif policy == "block":
            await queue.put(batch)
            self._points_routed += len(batch)

    # ------------------------------------------------------------------
    # private — sink consumer
    # ------------------------------------------------------------------

    async def _sink_consumer(self, sink_name: str, sink: SinkPort) -> None:
        """Per-sink consumer task — drains queue and writes to SinkPort."""
        queue = self._queues[sink_name]
        try:
            while True:
                batch = await queue.get()
                if not batch:  # empty list = shutdown sentinel
                    break
                try:
                    await sink.write(batch)
                except Exception:
                    logger.warning(
                        "Sink '%s' write failed for %d points",
                        sink_name,
                        len(batch),
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            logger.info("Sink consumer '%s' cancelled", sink_name)
            raise
