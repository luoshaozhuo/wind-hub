"""CollectorRuntime —— 采集系统三个子 Runtime 之间的整体协调器。

架构位置：application 层。职责：

- 设备子系统——委托 :class:`DeviceRuntime` 管理会话、连接状态与协议生命周期；
- Task 子系统——委托 :class:`TaskRuntime` 管理 Task Definition 展开、实例
  启停、采集句柄与采集执行状态；
- Sink 子系统——委托 :class:`SinkRuntime` 管理 Sink 生命周期、队列、
  消费者、背压与派发；
- 设备与 Sink 的增删 / 重建，配置热重载时的跨子系统顺序（:meth:`reconfigure`）；
- CollectorRuntime 状态（running / 聚合 health / 组件计数 / 点位统计）；
- 整体 ``start()`` / ``stop()`` 的子系统顺序。

持有：``DeviceRuntime``（设备会话与连接状态的唯一权威）、``TaskRuntime``
（Task / TaskInstance / 采集句柄 / 采集执行状态的唯一权威）、
``SinkRuntime``（Sink 注册表 / 队列 / 消费者 / 背压 / 派发的唯一权威）、
:class:`~wind_hub_collector.domain.acquisition.AcquisitionEngine`
（PointValue 数据流处理）。

不负责：协议实现细节（ProtocolPort 适配器）、配置加载与 diff
（CollectorConfigService）、采集时序（acquisition handle）、Sink 交付细节
（SinkRuntime）。

失败语义：设备连接与 sink 打开均为 best-effort——单个失败记录日志并跳过，
其余组件照常启动，失败组件经 :meth:`health` 暴露为不健康。
"""

from __future__ import annotations  # noqa: I001

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from functools import partial
from typing import Protocol

from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.application.runtime.acquisition_state import AcquisitionRuntimeState
from wind_hub_collector.application.runtime.device import CollectorDeviceSession
from wind_hub_collector.application.runtime.device_runtime import DeviceRuntime
from wind_hub_collector.application.runtime.device_state import DeviceRuntimeState
from wind_hub_collector.application.runtime.sink_runtime import SinkRuntime
from wind_hub_collector.application.runtime.task_instance import (
    CollectionTaskInstance,
    TaskInstanceState,
)
from wind_hub_collector.application.runtime.task_runtime import TaskRuntime
from wind_hub_collector.domain.acquisition.engine import AcquisitionEngine
from wind_hub_core.config import (
    CollectionTaskConfig,
    Config,
    DeviceConfig,
    PointConfig,
    ResolvedSinkConfig,
    RuntimeConfig,
)
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.reload import ConfigDiff, DeviceDiff, SinkDiff, TaskDiff
from wind_hub_core.protocol.port import AcquisitionMode, ProtocolPort

logger = logging.getLogger(__name__)

class RuntimeMetricsPort(Protocol):
    """运行时指标端口——由组合根注入（接 Prometheus 计数器/直方图）。

    只承载「事件发生时累加」的计数与观测（connect 失败、重连成功、
    collect 完成、poll 时序统计）；gauge 类状态（设备连通数、sink 队列
    深度）由 ``/metrics`` 拉取时从 CollectorRuntime 快照覆盖，不经本端口。
    application 层不直接依赖 infra 的 metrics 模块——保持与引擎回调
    一致的依赖倒置。
    """

    def acquisition_run_finished(
        self, device_id: str, group: str, outcome: str, duration: float | None
    ) -> None:
        """一次 collect 结束；``outcome`` ∈ ``{"success", "partial", "failed"}``。"""
        ...

    def acquisition_poll_stats(
        self, device_id: str, group: str, jitter: float, overrun: bool, missed: int
    ) -> None:
        """一次 fixed-rate poll 的时序统计。

        ``jitter`` = 实际启动时刻 − 计划时刻（monotonic 口径）；``overrun``
        表示本轮结束越过了下一个计划时刻；``missed`` 为本次跳过的完整周期
        数（catch-up 至多一次，其余槽位跳过）。
        """
        ...

    def device_connect_failed(self, device_id: str, protocol: str) -> None:
        """一次 connect 尝试失败（启动 / 热增 / 重建 / 重连节流窗口内）。"""
        ...

    def device_reconnected(self, device_id: str, protocol: str) -> None:
        """断线设备经 ensure 路径重连成功（驱动内部自重连不经 CollectorRuntime，不计入）。"""
        ...


class CollectorRuntime:
    """Collector 整体生命周期协调器——Device/Task/Sink 三个子 Runtime 的装配与编排。

    注入依赖（构造期均为纯内存装配，无网络 I/O）：

    - ``devices`` — 初始设备注册表，构造时把所有权交给 DeviceRuntime；
    - ``sinks`` — Sink 注册表，构造时把所有权交给 SinkRuntime；
    - ``engine`` — 采集引擎；本类构造时把三个状态端口分别绑定到对应
      子 Runtime（设备 → DeviceRuntime，采集 → TaskRuntime，派发 → SinkRuntime）；
    - ``tasks`` — 采集 Task Definition 注册表（``{task_id: config}``），
      构造时把所有权交给 TaskRuntime；
    - ``config`` — ``RuntimeConfig``（队列容量、背压策略、超时——透传给
      各子 Runtime，本类只保留整体快照）；
    - ``protocol_factory`` / ``sink_factory`` — 热重载重建组件用的工厂
      （由组合根注入；分别转交 DeviceRuntime / SinkRuntime，不在本类保存）。
    """

    def __init__(
        self,
        devices: dict[str, CollectorDeviceSession],
        sinks: dict[str, SinkPort],
        engine: AcquisitionEngine,
        config: RuntimeConfig,
        tasks: dict[str, CollectionTaskConfig] | None = None,
        protocol_factory: Callable[[DeviceConfig], ProtocolPort] | None = None,
        sink_factory: Callable[[ResolvedSinkConfig], SinkPort] | None = None,
        clock: Callable[[], float] = time.monotonic,
        metrics_hook: RuntimeMetricsPort | None = None,
    ) -> None:
        self._device_runtime = DeviceRuntime(
            devices, config, protocol_factory, clock, metrics_hook
        )
        self._task_runtime = TaskRuntime(
            tasks or {}, self._device_runtime, engine, clock, metrics_hook
        )
        self._sink_runtime = SinkRuntime(sinks, config, sink_factory)
        self._engine = engine
        self._config = config

        # 设备连接状态的落点：引擎采集前经 ensure_connected 完成带节流的
        # 重连，采集后上报 read 结果；设备端口直接绑定唯一 owner。
        self._engine.attach_device_state(self._device_runtime)
        # 采集执行状态的落点：引擎上报每次 collect 的开始/成功/失败
        # （TaskRuntime 实现 AcquisitionStatePort）。
        self._engine.attach_acquisition_state(self._task_runtime)
        # Sink 派发的落点：引擎采集结果进入 SinkRuntime 的队列/背压/消费者机制。
        self._engine.attach_sink_dispatch(self._sink_runtime)

        # 生命周期串行化：start / stop 不能重叠，保证启动中状态不会被停机
        # 直接覆写；``running`` 依然只在 ``_started`` 真正完成后才返回 True。
        self._lifecycle_lock = asyncio.Lock()
        self._running = False
        self._started = False

    # ------------------------------------------------------------------
    # 组件只读视图（CollectorQueryService / 适配器经此读取当前实例，热重载安全）
    # ------------------------------------------------------------------

    def attach_metrics_hook(
        self,
        metrics_hook: RuntimeMetricsPort | None,
    ) -> None:
        """注入或替换可选 RuntimeMetricsPort。

        Args:
            metrics_hook: 外部宿主提供的指标 observer；None 表示关闭事件指标。

        Notes:
            这是组合边界，不改变采集、控制或队列行为。
        """
        self._device_runtime.attach_metrics_hook(metrics_hook)
        self._task_runtime.attach_metrics_hook(metrics_hook)

    @property
    def device_runtime(self) -> DeviceRuntime:
        """设备子系统；设备查询与连接状态端口的唯一入口。"""
        return self._device_runtime

    @property
    def task_runtime(self) -> TaskRuntime:
        """Task 子系统；Task 查询、实例生命周期与采集执行状态的唯一入口。"""
        return self._task_runtime

    @property
    def sink_runtime(self) -> SinkRuntime:
        """Sink 子系统；Sink 注册表、队列、消费者与派发端口的唯一入口。"""
        return self._sink_runtime

    @property
    def devices(self) -> Mapping[str, CollectorDeviceSession]:
        """当前设备注册表的只读视图（随热重载就地反映最新内容）。

        所有权与变更均在 DeviceRuntime；外部只能观察，不得绕过 owner 修改。
        """
        return self._device_runtime.devices

    @property
    def sinks(self) -> Mapping[str, SinkPort]:
        """当前 Sink 注册表的只读视图（随热重载就地反映最新内容）。

        所有权与变更均在 SinkRuntime；外部只能观察，不得绕过 owner 修改。
        """
        return self._sink_runtime.sinks

    def device_state(self, device_id: str) -> DeviceRuntimeState | None:
        """单台设备的连接/重连簿记状态（委托 DeviceRuntime）。"""
        return self._device_runtime.device_state(device_id)

    def device_health(self) -> dict[str, HealthStatus]:
        """全部设备的健康状态（委托 DeviceRuntime）。"""
        return self._device_runtime.health()

    def sink_health(self) -> dict[str, HealthStatus]:
        """全部 Sink 的健康状态（委托 SinkRuntime）。"""
        return self._sink_runtime.health()

    @property
    def engine(self) -> AcquisitionEngine:
        """当前采集引擎（观察者注册、采集计数的入口）。"""
        return self._engine

    # ------------------------------------------------------------------
    # CollectorRuntime 整体生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动运行时——连接设备、打开 sink、注册采集 Task Instance（默认
        STOPPED，显式 start 才启动 acquisition）。

        子系统顺序：设备连接 → Sink 打开/消费者 → Task Instance 注册。
        单设备/单 Sink 失败只记录状态并继续，避免一个现场端点阻断整进程。
        """
        async with self._lifecycle_lock:
            if self._running:
                return
            self._running = True
            self._started = False

            await self._device_runtime.connect_all()
            await self._sink_runtime.start()
            # 注册采集 Task Instance（默认 STOPPED）——程序启动不自动开始
            # 采集，只有 gRPC 控制面的显式 start 才启动 acquisition。
            await self._task_runtime.sync_instances()

            self._started = True

    async def stop(self) -> None:
        """优雅停机——关闭全部 acquisition handle、排空队列、flush 并关闭
        sink、关闭设备连接。

        子系统按依赖逆序：先停采集（不再产生数据），再排空/关闭 Sink，
        最后关闭设备连接。单资源清理异常被记录但不阻断其他资源释放。
        """
        async with self._lifecycle_lock:
            if not self._running:
                return
            self._running = False
            self._started = False

            # 关闭全部实例采集句柄——polling 协程取消、订阅注销，不留
            # 后台 task 或订阅；实例定义保留并统一标记 STOPPED。
            await self._task_runtime.stop_all()
            await self._sink_runtime.stop()
            await self._device_runtime.close_all()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    def health(self) -> dict[str, HealthStatus]:
        """返回全部设备与 sink 的健康状态（设备优先、随后 sink）。"""
        result = self._device_runtime.health()
        result.update(self._sink_runtime.health())
        return result

    @property
    def running(self) -> bool:
        """运行时是否完整就绪。

        ``True`` 仅在 :meth:`start` 完成全部步骤（设备连接尝试、sink
        打开、Task Instance 注册）之后、:meth:`stop` 开始之前。启动进行中
        （如不可达设备仍在 ``connect_timeout`` 内）为 ``False``，使
        ``/health`` 能区分「启动中」与「已就绪」。
        """
        return self._running and self._started

    @property
    def device_count(self) -> int:
        """返回当前 CollectorRuntime 注册的设备数量。"""
        return len(self._device_runtime.devices)

    @property
    def sink_count(self) -> int:
        """返回当前 CollectorRuntime 注册的 Sink 数量。"""
        return len(self._sink_runtime.sinks)

    @property
    def points_collected(self) -> int:
        """累计采集点数（引擎侧口径）。"""
        return self._engine.points_collected

    @property
    def points_routed(self) -> int:
        """累计派发点数——成功进入 sink 队列的点值总数（SinkRuntime 口径）。"""
        return self._sink_runtime.points_routed

    @property
    def points_dropped(self) -> int:
        """累计丢弃点数——背压策略丢弃的点值总数（SinkRuntime 口径）。"""
        return self._sink_runtime.points_dropped

    def sink_queue_depths(self) -> dict[str, int]:
        """各 sink 队列当前深度——``/metrics`` 拉取时覆盖 ``sink_queue_depth``
        gauge（只读透传 SinkRuntime，SinkPort 自身不感知队列）。"""
        return self._sink_runtime.queue_depths()

    # ------------------------------------------------------------------
    # 采集 Task——查询与实例生命周期委托（实现与簿记在 TaskRuntime）
    # ------------------------------------------------------------------

    def task_definitions(self) -> dict[str, CollectionTaskConfig]:
        """当前 Task Definition 注册表（浅拷贝，委托 TaskRuntime）。"""
        return self._task_runtime.task_definitions()

    def task_instances(self) -> dict[str, CollectionTaskInstance]:
        """当前展开后的 Task Instance 注册表（浅拷贝，委托 TaskRuntime）。"""
        return self._task_runtime.task_instances()

    def instance_states(self) -> dict[str, TaskInstanceState]:
        """各 Task Instance 的生命周期状态（浅拷贝，委托 TaskRuntime）。"""
        return self._task_runtime.instance_states()

    async def start_task_instance(self, instance_id: str) -> None:
        """启动单个 Task Instance 的持续采集（委托 TaskRuntime）。

        Raises:
            KeyError: ``instance_id`` 不存在。
            Exception: 采集启动失败——状态保持 STOPPED，错误原样上抛。
        """
        await self._task_runtime.start_instance(instance_id)

    async def stop_task_instance(self, instance_id: str) -> None:
        """停止单个 Task Instance 的持续采集（委托 TaskRuntime）。

        Raises:
            KeyError: ``instance_id`` 不存在。
        """
        await self._task_runtime.stop_instance(instance_id)

    # ------------------------------------------------------------------
    # 采集执行状态（簿记在 TaskRuntime——引擎的 collect 钩子直接绑定它）
    # ------------------------------------------------------------------

    def acquisition_states(self) -> dict[str, AcquisitionRuntimeState]:
        """当前采集实例执行状态簿（``{instance_id: state}`` 浅拷贝，
        委托 TaskRuntime，CollectorQueryService 用）。"""
        return self._task_runtime.acquisition_states()

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
        """运行时新增设备；重复应用同一目标配置时安全收敛。

        部分 reload 失败后下一次会重放同一 diff。若设备已经按目标配置存在，
        不重复替换协议实例，只立即重试连接；若同 ID 但配置不同，则走 rebuild。
        """
        if self._device_runtime.requires_rebuild(device_id, cfg, points):
            await self.rebuild_device(device_id, cfg, protocol, points)
            return
        await self._device_runtime.add_device(device_id, cfg, protocol, points)
        # 新设备或重放热增可能影响 device_group Task 的展开。
        await self._task_runtime.sync_instances()

    async def remove_device(self, device_id: str) -> None:
        """运行时移除设备——停止并注销其全部采集实例并关闭连接。"""
        await self._device_runtime.remove_device(device_id)
        await self._task_runtime.sync_instances()
        logger.info("Hot-reload: device '%s' removed", device_id)

    async def rebuild_device(
        self,
        device_id: str,
        new_cfg: DeviceConfig,
        new_protocol: ProtocolPort,
        points: list[PointConfig],
    ) -> None:
        """重建设备——关闭旧连接，换入新配置/驱动后重新接入。

        正在运行的相关 TaskInstance：先关闭旧采集句柄，CollectorDeviceSession 重建完成
        后重新启动原本 RUNNING 的实例，保持其原运行状态。
        """
        was_running = await self._task_runtime.suspend_for_device_change(device_id)

        await self._device_runtime.rebuild_device(device_id, new_cfg, new_protocol, points)

        # device_group / enabled 可能随新配置变化——重新展开采集实例。
        await self._task_runtime.sync_instances()

        # 恢复原本 RUNNING 的实例（仍存在于此设备的展开结果中）。
        await self._task_runtime.resume_after_device_change(was_running)

    # ------------------------------------------------------------------
    # 热重载编排（CollectorConfigService 的唯一入口）
    # ------------------------------------------------------------------

    def convergence_diff(self, target: Config) -> ConfigDiff:
        """基于真实 CollectorRuntime 注册表生成强制收敛 diff。

        用于失败回滚或 revision reconciliation。它不依赖 CollectorConfigService 的
        current_config 基线，而是按当前实际设备/Sink/Task 注册表与目标配置
        生成一个保守 diff，确保曾被部分 reconfigure 修改的运行态能够重新
        收敛到目标配置。
        """
        target_devices = {item.device_id: item for item in target.devices.devices}
        actual_device_ids = set(self._device_runtime.devices)
        target_device_ids = set(target_devices)
        devices = DeviceDiff(
            added=sorted(target_device_ids - actual_device_ids),
            removed=sorted(actual_device_ids - target_device_ids),
            updated=sorted(target_device_ids & actual_device_ids),
        )

        target_sinks = {item.name: item for item in target.sinks.sinks if item.enabled}
        actual_sink_ids = set(self._sink_runtime.sinks)
        target_sink_ids = set(target_sinks)
        sinks = SinkDiff(
            added=sorted(target_sink_ids - actual_sink_ids),
            removed=sorted(actual_sink_ids - target_sink_ids),
            updated=sorted(target_sink_ids & actual_sink_ids),
        )

        target_task_ids = {item.task_id for item in target.tasks.tasks}
        actual_task_ids = set(self._task_runtime.task_definitions())
        tasks = TaskDiff(
            added=sorted(target_task_ids - actual_task_ids),
            removed=sorted(actual_task_ids - target_task_ids),
            updated=sorted(target_task_ids & actual_task_ids),
        )

        changed_tables = sorted(target.point_tables.tables)
        return ConfigDiff(
            devices=devices,
            sinks=sinks,
            tasks=tasks,
            points_changed=bool(changed_tables),
            point_tables_changed=changed_tables,
            units_changed=True,
        )

    async def reconfigure(self, new_config: Config, diff: ConfigDiff) -> list[str]:
        """按 diff 重构运行时——设备/sink/task 增删重建与点表重注入。

        各阶段相互隔离：单阶段失败记录到返回的错误列表，其余阶段继续执行。
        本方法不修改配置快照——``current_config`` 的提交时机由
        CollectorConfigService 决定。本类只负责跨子系统顺序与点表重注入；设备/sink
        子系统内部细节分别在 DeviceRuntime / SinkRuntime。

        Args:
            new_config: 已加载并通过校验的新配置。
            diff: 新旧配置的 diff（由 CollectorConfigService 计算）。

        Returns:
            错误描述列表；空列表表示全部阶段成功。
        """
        errors: list[str] = []

        # 在设备配置被就地更新前记录“point_table 绑定发生变化”的订阅设备。
        # 这类变化即使两张表内容本身都未修改，也必须重新注册 notification。
        restart_subscription_devices: set[str] = set()
        new_devices = {d.device_id: d for d in new_config.devices.devices}
        for did in diff.devices.updated:
            old_device = self._device_runtime.devices.get(did)
            new_device = new_devices.get(did)
            if (
                old_device is not None
                and new_device is not None
                and old_device.config.point_table != new_device.point_table
                and self._device_runtime.is_lightweight_update(did, new_device)
                and old_device.acquisition_mode is AcquisitionMode.SUBSCRIBE
            ):
                restart_subscription_devices.add(did)

        try:
            await self._apply_device_diff(diff, new_config)
        except Exception as exc:
            logger.error("Device diff apply failed: %s", exc, exc_info=True)
            errors.append(f"device: {exc}")

        try:
            await self._sink_runtime.apply_diff(
                diff.sinks, {s.name: s for s in new_config.sinks.sinks}
            )
        except Exception as exc:
            logger.error("Sink diff apply failed: %s", exc, exc_info=True)
            errors.append(f"sink: {exc}")

        # 点表内容变化：对绑定受影响表、且未在设备 diff 中增删重建的设备，
        # 仅重注入点映射——不重建 Protocol 连接。
        if diff.point_tables_changed:
            try:
                self._reinject_changed_tables(new_config, diff)
            except Exception as exc:
                logger.error("Point mapping re-inject failed: %s", exc, exc_info=True)
                errors.append(f"points: {exc}")

        # Task 定义变化：整体替换注册表并重新展开实例。设备/点表变化也可能
        # 改变展开结果（device_group 成员、enabled 翻转），统一在此收尾同步
        # ——实例增删只影响对应实例，不触碰任何 Protocol 连接。
        if diff.point_tables_changed:
            changed_tables = set(diff.point_tables_changed)
            for did, device in self._device_runtime.devices.items():
                if (
                    device.config.point_table in changed_tables
                    and device.acquisition_mode is AcquisitionMode.SUBSCRIBE
                ):
                    restart_subscription_devices.add(did)

        try:
            await self._task_runtime.apply_definitions(
                {t.task_id: t for t in new_config.tasks.tasks},
                restart_subscription_devices=restart_subscription_devices,
            )
        except Exception as exc:
            logger.error("Task instance sync failed: %s", exc, exc_info=True)
            errors.append(f"tasks: {exc}")

        return errors

    def _reinject_changed_tables(self, new_config: Config, diff: ConfigDiff) -> None:
        """点表内容变化时，对绑定受影响表的既有设备重注入点映射。

        仅重注入内存映射（``CollectorDeviceSession.set_points``——点表 + 协议映射），
        不触碰 Protocol 连接。新增/删除设备跳过；updated 设备若已经成功
        收敛到目标配置则仍会重注入，避免“轻量设备字段变化 + 点表内容变化”
        时遗漏新 mapping。
        """
        changed_tables = set(diff.point_tables_changed)
        removed = set(diff.devices.removed)
        added = set(diff.devices.added)
        target_devices = {d.device_id: d for d in new_config.devices.devices}

        for did, device in self._device_runtime.devices.items():
            if did in removed or did in added:
                continue
            target = target_devices.get(did)
            if target is None:
                continue
            # 设备更新阶段若未成功收敛到目标配置，不在这里继续叠加点表变化；
            # 成功的轻量更新/重建以及未更新设备都可安全重注入当前目标点表。
            if device.config.model_dump() != target.model_dump():
                continue
            if device.config.point_table not in changed_tables:
                continue
            self._device_runtime.set_points(did, self._points_for_device(new_config, did))

    async def _apply_device_diff(self, diff: ConfigDiff, new_cfg: Config) -> None:
        """按 diff 增删重建设备；新增/重建的协议实例由工厂创建。

        仅 ``point_table`` / ``device_group`` 变化的设备走轻量路径——就地
        更新配置、按需重注入点映射，不重建 Protocol 连接。
        """
        new_devices = {d.device_id: d for d in new_cfg.devices.devices}

        lightweight: set[str] = set()
        for did in diff.devices.updated:
            if self._device_runtime.is_lightweight_update(did, new_devices[did]):
                lightweight.add(did)

        if diff.devices.added or set(diff.devices.updated) - lightweight:
            self._device_runtime.require_protocol_factory()

        for did in diff.devices.removed:
            await self.remove_device(did)

        for did in diff.devices.added:
            cfg = new_devices[did]
            protocol = self._device_runtime.create_protocol(cfg)
            await self.add_device(did, cfg, protocol, self._points_for_device(new_cfg, did))

        for did in diff.devices.updated:
            cfg = new_devices[did]
            if did in lightweight:
                self._device_runtime.update_device(
                    did, cfg, partial(self._points_for_device, new_cfg, did)
                )
                continue
            protocol = self._device_runtime.create_protocol(cfg)
            await self.rebuild_device(did, cfg, protocol, self._points_for_device(new_cfg, did))

    @staticmethod
    def _points_for_device(config: Config, device_id: str) -> list[PointConfig]:
        """取设备绑定点表中的点列表（设备无关点表经绑定解析）。"""
        return config.points_for_device(device_id)
