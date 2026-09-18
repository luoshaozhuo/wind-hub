"""AcquisitionEngine —— 一次完整采集执行链。

架构位置：domain 层。职责严格限定为「执行一次采集」：

``Protocol read → Pipeline → Router → Sink 派发``

不负责：

- 「什么时候执行」——那是 :class:`~wind_hub.domain.port.scheduling.SchedulerPort`
  的职责，由 Runtime 把本引擎的 :meth:`collect` 注册为调度 Job；
- Protocol / Sink 实例的创建、连接与关闭——那是 Runtime 的生命周期职责；
- Sink 队列、背压与消费者任务——经 :class:`SinkDispatchPort` 端口委托给
  实现方（Runtime），本引擎只见「把路由结果派发出去」这一抽象。

并发语义：:meth:`collect` 可能被调度器按周期并发触发（不同设备/分组之间），
同一 Job 由调度端口保证不重入；本引擎自身无锁，依赖各 Port 实现的并发安全。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from wind_hub.config.schema import PointConfig
from wind_hub.domain.model.point import PointRef, PointValue
from wind_hub.domain.port.outbound import ProtocolPort
from wind_hub.domain.processing.pipeline import Pipeline
from wind_hub.domain.routing.router import Router

logger = logging.getLogger(__name__)


class SinkDispatchPort(Protocol):
    """Sink 派发端口——把路由结果交给 Sink 侧（队列/背压/消费者）。

    由 Runtime 实现：引擎不感知队列与背压策略，只保证「路由到哪个 sink、
    批次内容是什么」如实传递。派发过程中的丢弃/阻塞语义由实现方定义。
    """

    async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
        """派发一批按 sink 名分组的路由结果。

        Args:
            routed: ``{sink_name: [PointValue, …]}``；空批次由实现方忽略。
        """
        ...


class AcquisitionEngine:
    """采集引擎——执行单次「读 → 处理 → 路由 → 派发」链路。

    注入依赖：

    - ``protocols`` — 按 device_id 索引的协议驱动注册表（与 Runtime 共享同一
      dict，Runtime 热重载时就地增删，引擎总是读到当前实例）；
    - ``pipeline`` / ``router`` — 当前处理链与路由表实例，可被
      :meth:`replace_pipeline` / :meth:`replace_router` 原子替换；
    - ``points_by_device`` — 按 device_id 分组的点表（同样与 Runtime 共享）；
    - ``on_points_collected`` — 采集计数回调（组合根接 Prometheus 计数器，
      domain 不依赖 infra）。

    运行期统计（决策 7 口径）：``points_collected`` 在
    :meth:`process_and_route` 入口统一计数，轮询与订阅推送两条路径口径一致；
    路由/丢弃计数属于 Sink 派发侧（Runtime）。
    """

    def __init__(
        self,
        protocols: dict[str, ProtocolPort],
        pipeline: Pipeline,
        router: Router,
        points_by_device: dict[str, list[PointConfig]] | None = None,
        on_points_collected: Callable[[int], None] | None = None,
    ) -> None:
        self._protocols = protocols
        self._pipeline = pipeline
        self._router = router
        self._points_by_device = points_by_device if points_by_device is not None else {}
        self._on_points_collected = on_points_collected or (lambda _n: None)

        # Sink 派发端口由 Runtime 在装配完成后注入（engine 先于 runtime 创建，
        # 无法构造期传入）；未绑定前调用 collect/process_and_route 会抛
        # RuntimeError——组合根保证绑定发生在任何采集触发之前。
        self._sink_dispatch: SinkDispatchPort | None = None

        # 同步观察者：每批处理后的点值全量通知（如 IEC104 从站快照、perf
        # 延迟观测）。观察者异常被隔离，绝不影响采集链路。
        self._observers: list[Callable[[list[PointValue]], None]] = []

        self._points_collected = 0

    # ------------------------------------------------------------------
    # 装配缝（composition root / Runtime 专用）
    # ------------------------------------------------------------------

    def attach_sink_dispatch(self, dispatch: SinkDispatchPort) -> None:
        """绑定 Sink 派发端口——由 Runtime 在自身构造时调用一次。

        引擎先于 Runtime 创建（组合根顺序），无法在构造期拿到 Runtime 的
        派发实现，因此采用显式后绑定；绑定是幂等的一次性装配动作。
        """
        self._sink_dispatch = dispatch

    def add_observer(self, callback: Callable[[list[PointValue]], None]) -> None:
        """注册采集观察者——每批处理完成后同步回调一次。

        观察者在事件循环上同步执行，异常被捕获并记录，不会干扰采集管线。
        """
        self._observers.append(callback)

    # ------------------------------------------------------------------
    # 采集执行
    # ------------------------------------------------------------------

    async def collect(self, device_id: str, group: str) -> None:
        """执行一次轮询采集：读设备点表 → 处理 → 路由 → 派发。

        这是调度 Job 的执行体入口。单次采集失败（设备不可达、驱动异常等）
        只记录 warning，不上抛——否则一次失败会让调度 Job 持续打堆栈甚至
        影响其他 Job；下个周期会自然重试，与旧设备循环的容错语义一致。

        Args:
            device_id: 目标设备。
            group: 轮询分组名（当前 ``PointConfig`` 无分组标签，仅用于日志
                与 Job 标识，不做点表过滤）。

        Raises:
            RuntimeError: Sink 派发端口尚未绑定（装配未完成）。
        """
        proto = self._protocols.get(device_id)
        if proto is None:
            logger.warning("采集跳过：设备 '%s' 无协议驱动", device_id)
            return
        refs = [
            PointRef(device_id=device_id, point_id=p.point_id)
            for p in self._points_by_device.get(device_id, [])
        ]
        try:
            batch = await proto.read(refs)
        except Exception:
            logger.warning(
                "设备 '%s' 分组 '%s' 轮询失败——下个周期重试",
                device_id,
                group,
                exc_info=True,
            )
            return
        await self.process_and_route(batch)

    async def process_and_route(self, batch: list[PointValue]) -> None:
        """处理 → 路由 → 派发一批点值（订阅推送路径同样经此入口）。

        采集统计在本方法入口统一计数（决策 0.1）：轮询（:meth:`collect`
        调用）与订阅推送（Runtime 的订阅回调调用）两条路径都经过这里，因此
        ``points_collected`` 与注入的 ``on_points_collected`` 回调口径一致。

        Raises:
            RuntimeError: Sink 派发端口尚未绑定（装配未完成）。
        """
        if not batch:
            return
        if self._sink_dispatch is None:
            raise RuntimeError(
                "AcquisitionEngine 未绑定 Sink 派发端口——组合根未完成装配"
            )
        self._points_collected += len(batch)
        self._on_points_collected(len(batch))
        processed = await self._pipeline.process(batch)
        self._notify_observers(processed)
        routed = self._router.route(processed)
        await self._sink_dispatch.dispatch(routed)

    # ------------------------------------------------------------------
    # 当前实例管理（Runtime 热替换的落点）
    # ------------------------------------------------------------------

    async def replace_router(self, new_router: Router) -> None:
        """原子替换路由表（点表/规则变更时由 Runtime 调用）。"""
        self._router = new_router
        logger.info("Router replaced (%d entries)", new_router.table_size)

    async def replace_pipeline(self, new_pipeline: Pipeline) -> None:
        """原子替换处理链（处理器列表或点表变更时由 Runtime 调用）。"""
        self._pipeline = new_pipeline
        logger.info("Pipeline replaced (%d processors)", new_pipeline.processor_count)

    @property
    def current_router(self) -> Router:
        """返回当前路由表实例——热替换后调用方立即看到新实例。"""
        return self._router

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def points_collected(self) -> int:
        """累计采集点数——进入处理管线的点值总数，含轮询与订阅推送
        （单调不减，决策 7 / 0.1 口径统一）。"""
        return self._points_collected

    # ------------------------------------------------------------------
    # 私有
    # ------------------------------------------------------------------

    def _notify_observers(self, values: list[PointValue]) -> None:
        """逐个通知观察者，隔离其异常——观察者失败不得影响采集链路。"""
        for observer in self._observers:
            try:
                observer(values)
            except Exception:
                logger.warning("Observer raised during collection — ignored", exc_info=True)
