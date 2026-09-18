"""Runtime —— 采集系统的运行时组件管理与生命周期编排核心。

架构位置：application 层。职责：

- Protocol 实例生命周期（连接 / 关闭 / 重建）；
- Sink 生命周期（打开 / 队列 / 消费者任务 / 背压 / 关闭）；
- Pipeline / Router 当前实例管理（经 :class:`AcquisitionEngine` 原子替换）；
- 设备与 Sink 的增删 / 重建，配置热重载时的运行时重构（:meth:`reconfigure`）；
- Runtime 状态（running / health / 组件计数 / 点位统计）；
- 整体 ``start()`` / ``stop()``。

持有：:class:`~wind_hub.domain.port.scheduling.SchedulerPort`（决定「何时
执行」）、:class:`~wind_hub.domain.acquisition.AcquisitionEngine`（执行
「一次采集」）、:class:`~wind_hub.domain.command.Dispatcher`（命令分发）。

不负责：具体时间调度算法（SchedulerPort 的实现细节）、协议实现细节
（ProtocolPort 适配器）、配置加载与 diff（ConfigService）。

失败语义：设备连接与 sink 打开均为 best-effort——单个失败记录日志并跳过，
其余组件照常启动，失败组件经 :meth:`health` 暴露为不健康。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from wind_hub.config.routing import RoutingTable
from wind_hub.config.schema import (
    Config,
    DeviceConfig,
    PointConfig,
    PollingGroup,
    SchedulerConfig,
    SinkConfig,
)
from wind_hub.domain.acquisition.engine import AcquisitionEngine
from wind_hub.domain.command.dispatcher import Dispatcher
from wind_hub.domain.model.point import PointRef, PointValue
from wind_hub.domain.model.reload import ConfigDiff
from wind_hub.domain.port.outbound import (
    HealthStatus,
    ProcessorPort,
    ProtocolPort,
    SinkPort,
)
from wind_hub.domain.port.scheduling import SchedulerPort
from wind_hub.domain.processing.pipeline import Pipeline
from wind_hub.domain.routing.router import Router

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


class Runtime:
    """运行时——组件注册表、生命周期与状态的唯一权威。

    注入依赖（构造期均为纯内存装配，无网络 I/O）：

    - ``devices`` / ``protocols`` / ``sinks`` — 组件注册表；与
      ``AcquisitionEngine`` 共享同一 dict，热重载就地增删后双方立即可见；
    - ``engine`` — 采集引擎；本类构造时向其绑定 Sink 派发端口；
    - ``scheduler`` — 调度端口；轮询 Job 以 ``poll:{device_id}:{group}``
      为 id 注册，执行体为 ``engine.collect``；
    - ``dispatcher`` — 命令分发器（持有以便组合根单点管理生命周期）；
    - ``config`` — ``SchedulerConfig``（队列容量、背压策略、超时）；
    - ``points_by_device`` — 按设备分组的点表；
    - ``protocol_factory`` / ``sink_factory`` / ``processor_factory`` —
      热重载重建组件用的工厂（由组合根注入，Runtime 不依赖具体适配器）。
    """

    def __init__(
        self,
        devices: dict[str, DeviceConfig],
        protocols: dict[str, ProtocolPort],
        sinks: dict[str, SinkPort],
        engine: AcquisitionEngine,
        scheduler: SchedulerPort,
        dispatcher: Dispatcher,
        config: SchedulerConfig,
        points_by_device: dict[str, list[PointConfig]] | None = None,
        protocol_factory: Callable[[DeviceConfig], ProtocolPort] | None = None,
        sink_factory: Callable[[SinkConfig], SinkPort] | None = None,
        processor_factory: Callable[[str, list[PointConfig]], ProcessorPort] | None = None,
    ) -> None:
        self._devices = devices
        self._protocols = protocols
        self._sinks = sinks
        self._engine = engine
        self._scheduler = scheduler
        self._dispatcher = dispatcher
        self._config = config
        self._points_by_device = points_by_device if points_by_device is not None else {}
        self._protocol_factory = protocol_factory
        self._sink_factory = sink_factory
        self._processor_factory = processor_factory

        # Sink 派发的落点：引擎路由结果进入本类的队列/背压/消费者机制。
        self._engine.attach_sink_dispatch(self)

        # Per-sink bounded queues
        self._queues: dict[str, asyncio.Queue[list[PointValue]]] = {
            name: asyncio.Queue(maxsize=config.queue_maxsize) for name in sinks
        }

        # Sink 消费者任务簿记（设备侧由调度器 Job 承担，不再有设备任务）
        self._sink_tasks: dict[str, asyncio.Task[Any]] = {}
        self._running = False
        # ``start()`` 完整完成（设备连接尝试结束、任务已拉起）才置位；
        # ``running`` 属性把它与 ``_running`` 取与，让 /health 能区分
        # 「启动进行中」（决策 1）与「已就绪」。
        self._started = False

        # Sinks whose ``open()`` raised during ``start()`` — they are skipped
        # and surfaced as unhealthy by :meth:`health`.
        self._unhealthy_sinks: set[str] = set()

        # 运行期统计（决策 7）：路由/丢弃在 Sink 派发侧计数；
        # 采集计数在引擎侧（经 ``points_collected`` 属性透传）。
        self._points_routed = 0
        self._points_dropped = 0

    # ------------------------------------------------------------------
    # 组件只读视图（QueryService / 适配器经此读取当前实例，热重载安全）
    # ------------------------------------------------------------------

    @property
    def devices(self) -> dict[str, DeviceConfig]:
        """当前设备配置注册表（热重载后就地反映最新内容）。"""
        return self._devices

    @property
    def protocols(self) -> dict[str, ProtocolPort]:
        """当前协议驱动注册表。"""
        return self._protocols

    @property
    def points_by_device(self) -> dict[str, list[PointConfig]]:
        """当前按设备分组的点表。"""
        return self._points_by_device

    @property
    def engine(self) -> AcquisitionEngine:
        """当前采集引擎（观察者注册、采集计数的入口）。"""
        return self._engine

    @property
    def dispatcher(self) -> Dispatcher:
        """命令分发器。"""
        return self._dispatcher

    @property
    def current_router(self) -> Router:
        """当前路由表实例——热替换后立即可见。"""
        return self._engine.current_router

    # ------------------------------------------------------------------
    # Runtime 整体生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动运行时——连接设备、打开 sink、启动调度器并注册采集 Job。

        单台设备连接失败或单个 sink 打开失败只记录日志并跳过，其余组件
        照常启动。幂等：已运行时重复调用为无操作。
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
        #    the runtime still starts; it is surfaced as unhealthy by health()).
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

        # 4. Start the scheduler, then register acquisition per device
        await self._scheduler.start()
        for device_id, device_cfg in self._devices.items():
            await self._start_acquisition(device_id, device_cfg)

        # 全部启动步骤完成后才对外报告 running（决策 1：/health 可区分
        # 「启动进行中」——设备连接超时期间 running 保持 False）。
        self._started = True

    async def stop(self) -> None:
        """优雅停机——停调度、排空队列、flush 并关闭 sink、关闭设备连接。

        幂等：未运行时直接返回。各环节失败只记录日志，保证停机链路走完。
        """
        if not self._running:
            return
        self._running = False
        self._started = False

        # 1. Stop the scheduler — no new job fires from here on
        await self._scheduler.stop()

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
    # 状态
    # ------------------------------------------------------------------

    def health(self) -> dict[str, HealthStatus]:
        """返回全部设备与 sink 的健康状态（设备优先、随后 sink）。"""
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
        """运行时是否完整就绪。

        ``True`` 仅在 :meth:`start` 完成全部步骤（设备连接尝试、sink
        打开、调度器与 Job 注册）之后、:meth:`stop` 开始之前。启动进行中
        （如不可达设备仍在 ``connect_timeout`` 内）为 ``False``，使
        ``/health`` 能区分「启动中」与「已就绪」（决策 1）。
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
        """累计采集点数（引擎侧口径，含轮询与订阅推送）。"""
        return self._engine.points_collected

    @property
    def points_routed(self) -> int:
        """累计路由点数——成功进入 sink 队列的点值总数（单调不减，决策 7）。"""
        return self._points_routed

    @property
    def points_dropped(self) -> int:
        """累计丢弃点数——背压策略丢弃的点值总数（单调不减，决策 7）。"""
        return self._points_dropped

    # ------------------------------------------------------------------
    # Sink 派发端口实现（AcquisitionEngine → Runtime 的落点）
    # ------------------------------------------------------------------

    async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
        """把路由结果按 sink 入队，应用背压策略（实现 ``SinkDispatchPort``）。"""
        for sink_name, batch in routed.items():
            if not batch:
                continue
            queue = self._queues.get(sink_name)
            if queue is None:
                continue
            await self._handle_backpressure(queue, batch, sink_name)

    # ------------------------------------------------------------------
    # 热重载——设备管理
    # ------------------------------------------------------------------

    async def add_device(
        self,
        device_id: str,
        cfg: DeviceConfig,
        protocol: ProtocolPort,
        points: list[PointConfig],
    ) -> None:
        """运行时新增设备——注入点表、连接、注册采集 Job / 订阅。"""
        self._devices[device_id] = cfg
        self._protocols[device_id] = protocol
        protocol.set_points_mapping(points)
        self._points_by_device[device_id] = points

        try:
            await asyncio.wait_for(protocol.connect(), timeout=self._config.connect_timeout)
            logger.info("Hot-reload: device '%s' connected", device_id)
        except Exception:
            logger.warning(
                "Hot-reload: device '%s' failed to connect — task launched anyway",
                device_id,
                exc_info=True,
            )

        if cfg.enabled and self._running:
            await self._start_acquisition(device_id, cfg)

    async def remove_device(self, device_id: str) -> None:
        """运行时移除设备——注销其全部调度 Job 并关闭连接。"""
        self._remove_device_jobs(device_id)

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
        self._points_by_device.pop(device_id, None)
        logger.info("Hot-reload: device '%s' removed", device_id)

    async def rebuild_device(
        self,
        device_id: str,
        new_cfg: DeviceConfig,
        new_protocol: ProtocolPort,
        points: list[PointConfig],
    ) -> None:
        """重建设备——注销旧 Job、关闭旧连接，换入新配置/驱动后重新接入。"""
        self._remove_device_jobs(device_id)

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

        self._devices[device_id] = new_cfg
        self._protocols[device_id] = new_protocol
        new_protocol.set_points_mapping(points)
        self._points_by_device[device_id] = points

        try:
            await asyncio.wait_for(new_protocol.connect(), timeout=self._config.connect_timeout)
            logger.info("Hot-reload: device '%s' reconnected", device_id)
        except Exception:
            logger.warning(
                "Hot-reload: device '%s' connect failed after rebuild",
                device_id,
                exc_info=True,
            )

        if new_cfg.enabled and self._running:
            await self._start_acquisition(device_id, new_cfg)

    # ------------------------------------------------------------------
    # 热重载——sink 管理
    # ------------------------------------------------------------------

    async def add_sink(self, sink_name: str, cfg: SinkConfig, sink: SinkPort) -> None:
        """运行时新增 sink——打开、建队列、启动消费者。

        Raises:
            Exception: ``sink.open()`` 失败原样上抛（热重载编排方据此记录
                错误并保留旧状态）。
        """
        self._sinks[sink_name] = sink
        queue: asyncio.Queue[list[PointValue]] = asyncio.Queue(maxsize=self._config.queue_maxsize)
        self._queues[sink_name] = queue

        await sink.open()
        logger.info("Hot-reload: sink '%s' opened", sink_name)

        if self._running:
            task = asyncio.create_task(self._sink_consumer(sink_name, sink))
            self._sink_tasks[sink_name] = task

    async def remove_sink(self, sink_name: str) -> None:
        """运行时移除 sink——停消费者、flush、关闭、移除队列。"""
        task = self._sink_tasks.pop(sink_name, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=self._config.shutdown_timeout)

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
        self._unhealthy_sinks.discard(sink_name)
        logger.info("Hot-reload: sink '%s' removed", sink_name)

    async def rebuild_sink(self, sink_name: str, new_cfg: SinkConfig, new_sink: SinkPort) -> None:
        """重建 sink——停旧消费者、换入新实例、启动新消费者。

        既有队列保留，避免在途数据丢失。

        Raises:
            Exception: 新 sink 的 ``open()`` 失败原样上抛。
        """
        task = self._sink_tasks.pop(sink_name, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=self._config.shutdown_timeout)

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

        self._sinks[sink_name] = new_sink
        await new_sink.open()
        logger.info("Hot-reload: sink '%s' re-opened", sink_name)

        if self._running:
            new_task = asyncio.create_task(self._sink_consumer(sink_name, new_sink))
            self._sink_tasks[sink_name] = new_task

    # ------------------------------------------------------------------
    # 热重载——router / pipeline 替换
    # ------------------------------------------------------------------

    async def replace_router(self, new_router: Router) -> None:
        """原子替换路由表（点表/规则变更时）。Sink 队列不受影响。"""
        await self._engine.replace_router(new_router)

    async def replace_pipeline(self, new_pipeline: Pipeline) -> None:
        """原子替换处理链（处理器列表或点表变更时）。"""
        await self._engine.replace_pipeline(new_pipeline)

    # ------------------------------------------------------------------
    # 热重载编排（ConfigService 的唯一入口）
    # ------------------------------------------------------------------

    async def reconfigure(self, new_config: Config, diff: ConfigDiff) -> list[str]:
        """按 diff 重构运行时——设备/sink 增删重建、路由表与处理链替换。

        各阶段相互隔离：单阶段失败记录到返回的错误列表，其余阶段继续执行
        （与旧 ConfigService 的部分失败语义一致）。本方法不修改配置快照——
        ``current_config`` 的提交时机由 ConfigService 决定。

        Args:
            new_config: 已加载并通过校验的新配置。
            diff: 新旧配置的 diff（由 ConfigService 计算）。

        Returns:
            错误描述列表；空列表表示全部阶段成功。
        """
        errors: list[str] = []

        try:
            await self._apply_device_diff(diff, new_config)
        except Exception as exc:
            logger.error("Device diff apply failed: %s", exc, exc_info=True)
            errors.append(f"device: {exc}")

        try:
            await self._apply_sink_diff(diff, new_config)
        except Exception as exc:
            logger.error("Sink diff apply failed: %s", exc, exc_info=True)
            errors.append(f"sink: {exc}")

        if diff.points_changed or diff.rules_changed:
            try:
                table = RoutingTable(
                    new_config.routing.rules,
                    new_config.points.points,
                    new_config.routing.unmatched_policy,
                )
                await self.replace_router(Router(table))
                logger.info("Routing table rebuilt (%d entries)", table.size)
            except Exception as exc:
                logger.error("Routing table rebuild failed: %s", exc, exc_info=True)
                errors.append(f"routing: {exc}")

        # 点表变更也让 Processor 重新注入新点表（死区状态重置可接受）
        if (
            diff.pipeline_changed or diff.points_changed
        ) and self._processor_factory is not None:
            try:
                processors = [
                    self._processor_factory(name, new_config.points.points)
                    for name in new_config.system.pipeline.processors
                ]
                await self.replace_pipeline(Pipeline(processors))
                logger.info("Pipeline rebuilt (%d processors)", len(processors))
            except Exception as exc:
                logger.error("Pipeline rebuild failed: %s", exc, exc_info=True)
                errors.append(f"pipeline: {exc}")

        return errors

    async def _apply_device_diff(self, diff: ConfigDiff, new_cfg: Config) -> None:
        """按 diff 增删重建设备；新增/重建的协议实例由工厂创建。"""
        factory = self._protocol_factory
        if factory is None and (diff.devices.added or diff.devices.updated):
            raise RuntimeError("protocol factory is not wired into Runtime")
        new_devices = {d.device_id: d for d in new_cfg.devices.devices}

        for did in diff.devices.removed:
            await self.remove_device(did)

        for did in diff.devices.added:
            cfg = new_devices[did]
            # 入口已守卫：有新增/更新时 factory 必然非 None
            assert factory is not None
            protocol = factory(cfg)
            await self.add_device(did, cfg, protocol, self._points_for_device(new_cfg, did))

        for did in diff.devices.updated:
            cfg = new_devices[did]
            assert factory is not None  # 同上——入口守卫保证
            protocol = factory(cfg)
            await self.rebuild_device(did, cfg, protocol, self._points_for_device(new_cfg, did))

    @staticmethod
    def _points_for_device(config: Config, device_id: str) -> list[PointConfig]:
        """取某设备的完整点表（按 device_id 过滤）。"""
        return [p for p in config.points.points if p.device_id == device_id]

    async def _apply_sink_diff(self, diff: ConfigDiff, new_cfg: Config) -> None:
        """按 diff 增删重建 sink；新实例由工厂创建。"""
        factory = self._sink_factory
        if factory is None and (diff.sinks.added or diff.sinks.updated):
            raise RuntimeError("sink factory is not wired into Runtime")
        new_sinks = {s.name: s for s in new_cfg.system.sinks}

        for name in diff.sinks.removed:
            await self.remove_sink(name)

        for name in diff.sinks.added:
            cfg = new_sinks[name]
            # 入口已守卫：有新增/更新时 factory 必然非 None
            assert factory is not None
            sink = factory(cfg)
            await self.add_sink(name, cfg, sink)

        for name in diff.sinks.updated:
            cfg = new_sinks[name]
            assert factory is not None  # 同上——入口守卫保证
            sink = factory(cfg)
            await self.rebuild_sink(name, cfg, sink)

    # ------------------------------------------------------------------
    # 私有——采集接入（调度 Job 注册 / 订阅）
    # ------------------------------------------------------------------

    def _polling_groups(self, device_cfg: DeviceConfig) -> list[PollingGroup]:
        """返回设备的轮询分组；未配置时使用默认分组（默认间隔）。"""
        groups = list(device_cfg.polling)
        if not groups:
            groups = [PollingGroup(group="default", interval=self._config.default_interval)]
        return groups

    @staticmethod
    def _job_id(device_id: str, group: str) -> str:
        """轮询 Job 标识约定：``poll:{device_id}:{group}``。"""
        return f"poll:{device_id}:{group}"

    def _remove_device_jobs(self, device_id: str) -> None:
        """注销某设备的全部轮询 Job（按 id 前缀匹配）。

        未知 Job 的 ``remove_job`` 会抛 ``KeyError``——设备可能以纯订阅
        模式运行而没有轮询 Job，故缺席是正常情况，静默跳过。
        """
        prefix = f"poll:{device_id}:"
        for job in self._scheduler.list_jobs():
            if job.job_id.startswith(prefix):
                self._scheduler.remove_job(job.job_id)

    async def _start_acquisition(self, device_id: str, device_cfg: DeviceConfig) -> None:
        """按设备 ``mode`` 接入采集：``'poll'`` 注册周期 Job；
        ``'subscribe'`` 订阅推送；``'both'`` 两者兼有。禁用设备跳过。"""
        if not device_cfg.enabled:
            return
        if device_cfg.mode in ("poll", "both"):
            for group in self._polling_groups(device_cfg):
                self._scheduler.add_interval_job(
                    job_id=self._job_id(device_id, group.group),
                    interval_seconds=group.interval,
                    func=self._engine.collect,
                    args=(device_id, group.group),
                    replace_existing=True,
                )
        if device_cfg.mode in ("subscribe", "both"):
            await self._start_subscription(device_id, device_cfg)

    async def _start_subscription(self, device_id: str, device_cfg: DeviceConfig) -> None:
        """订阅设备推送（push 模式），推送值回流到正常采集链路。

        驱动不支持订阅（``NotImplementedError``）或订阅调用失败时，纯
        ``'subscribe'`` 设备回退为轮询 Job，保证仍能采集。
        """
        proto = self._protocols.get(device_id)
        if proto is None:
            return
        refs = [
            PointRef(device_id=device_id, point_id=p.point_id)
            for p in self._points_by_device.get(device_id, [])
        ]

        async def on_data(value: PointValue) -> None:
            await self._engine.process_and_route([value])

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
        """订阅失败后把纯订阅设备切换为轮询 Job。"""
        if device_cfg.mode == "subscribe":
            for group in self._polling_groups(device_cfg):
                self._scheduler.add_interval_job(
                    job_id=self._job_id(device_id, group.group),
                    interval_seconds=group.interval,
                    func=self._engine.collect,
                    args=(device_id, group.group),
                    replace_existing=True,
                )

    # ------------------------------------------------------------------
    # 私有——sink 背压与消费者
    # ------------------------------------------------------------------

    async def _handle_backpressure(
        self,
        queue: asyncio.Queue[list[PointValue]],
        batch: list[PointValue],
        sink_name: str,
    ) -> None:
        """按配置的背压策略把批次送入队列，并维护路由/丢弃计数。"""
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

    async def _sink_consumer(self, sink_name: str, sink: SinkPort) -> None:
        """Per-sink 消费者任务——从队列取批次并写入 SinkPort。"""
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
