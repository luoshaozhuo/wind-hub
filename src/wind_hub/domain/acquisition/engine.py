"""AcquisitionEngine —— 一次完整采集执行链。

架构位置：domain 层。职责严格限定为「执行一次采集」：

``Protocol read → Pipeline → Router → Sink 派发``

不负责：

- 「什么时候执行」——那是 :class:`~wind_hub.application.port.scheduling.SchedulerPort`
  的职责，由 Runtime 把本引擎的 :meth:`collect` 注册为调度 Job；
- Protocol / Sink 实例的创建、连接与关闭——那是 Runtime 的生命周期职责；
- Sink 队列、背压与消费者任务——经 :class:`SinkDispatchPort` 端口委托给
  实现方（Runtime），本引擎只见「把路由结果派发出去」这一抽象。

并发语义：:meth:`collect` 可能被调度器按周期并发触发（不同设备/分组之间），
同一 Job 由调度端口保证不重入；本引擎自身无锁，依赖各 Port 实现的并发安全。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from wind_hub.config.schema import PointConfig
from wind_hub.domain.model.point import PointRef, PointValue, Quality
from wind_hub.domain.port.outbound import ProtocolPort
from wind_hub.domain.processing.pipeline import Pipeline
from wind_hub.domain.routing.delivery import DeliveryDispatcher
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


class DeviceStatePort(Protocol):
    """设备连接状态端口——采集前确保连接、采集后上报结果。

    由 Runtime 实现：引擎**不管理 Protocol 生命周期**（不重连、不计
    失败次数），只在「读之前问一句能否读、读之后如实报告结果」。
    :meth:`ensure_connected` 返回 ``False``（断线且重连节流中）时本次
    采集直接跳过——断线设备不再发起注定失败的 read，也不形成连接风暴。
    """

    async def ensure_connected(self, device_id: str) -> bool:
        """确保设备可用；返回 ``False`` 表示本次应跳过采集。"""
        ...

    def report_read_success(self, device_id: str) -> None:
        """上报一次成功的批量读。"""
        ...

    def report_read_failure(self, device_id: str, error: BaseException) -> None:
        """上报一次失败的批量读（原始异常，由实现方做故障分类）。"""
        ...


class AcquisitionStatePort(Protocol):
    """采集执行状态端口——一次 collect 的开始/成功/失败上报。

    由 Runtime 实现（持有 ``{(device, group): AcquisitionRuntimeState}``）。
    与 :class:`DeviceStatePort` 分维度：本端口描述**业务执行**（这个采集
    Job 最近跑得怎样），不描述设备连接。调度器的 Job 注册/暂停状态属于
    第三维度（SchedulerPort），三者不混淆。

    一次 collect 的判定口径：

    - ``success``——批量读无异常且至少一个点有效（``partial=True`` 表示
      批次为 GOOD/BAD 混合，仍算成功、不计连续失败）；
    - ``failure``——读整体抛异常、读超时、断线跳过（不重发 read）、
      或批次没有任何有效结果（全 BAD / 空批）。
    """

    def report_collect_started(self, device_id: str, group: str) -> None:
        """一次 collect 开始（running=True）。"""
        ...

    def report_collect_success(self, device_id: str, group: str, *, partial: bool) -> None:
        """一次 collect 成功结束；``partial`` 表示批次含 BAD 点的混合结果。"""
        ...

    def report_collect_failure(self, device_id: str, group: str, error: str) -> None:
        """一次 collect 失败结束（描述已压成一行文本）。"""
        ...


class AcquisitionEngine:
    """采集引擎——执行单次「读 → 处理 → 路由 → 派发」链路。

    注入依赖：

    - ``protocols`` — 按 device_id 索引的协议驱动注册表（与 Runtime 共享同一
      dict，Runtime 热重载时就地增删，引擎总是读到当前实例）；
    - ``pipeline`` / ``router`` / ``delivery`` — 当前处理链、路由表与投递
      策略实例，可被 :meth:`replace_pipeline` / :meth:`replace_router` /
      :meth:`replace_delivery` 原子替换；
    - ``points_by_device`` — 按 device_id 分组的点表（同样与 Runtime 共享）；
    - ``on_points_collected`` / ``on_points_bad`` — 采集/BAD 点计数回调
      （组合根接 Prometheus 计数器，domain 不依赖 infra）；
    - ``read_timeout`` — 应用层读超时（外层兜底）；协议驱动内部的底层
      超时各自保留，``None`` 表示不加外层超时。

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
        on_points_bad: Callable[[int], None] | None = None,
        delivery: DeliveryDispatcher | None = None,
        read_timeout: float | None = None,
    ) -> None:
        self._protocols = protocols
        self._pipeline = pipeline
        self._router = router
        self._delivery = delivery
        self._points_by_device = points_by_device if points_by_device is not None else {}
        self._on_points_collected = on_points_collected or (lambda _n: None)
        # BAD 质量点计数回调（协议采集结果中的 Quality.BAD——数据质量问题，
        # 与背压丢弃 points_dropped 语义不同，分开计数）。
        self._on_points_bad = on_points_bad or (lambda _n: None)
        # 应用层读超时（外层兜底）：一次批量读允许占用的最大时间；协议驱动
        # 内部的底层 socket/协议超时各自保留，两层职责不同。``None`` 表示
        # 不加外层超时（组合根总是注入配置值）。
        self._read_timeout = read_timeout

        # Sink 派发端口由 Runtime 在装配完成后注入（engine 先于 runtime 创建，
        # 无法构造期传入）；未绑定前调用 collect/process_and_route 会抛
        # RuntimeError——组合根保证绑定发生在任何采集触发之前。
        self._sink_dispatch: SinkDispatchPort | None = None

        # 设备连接状态端口（可选）：同样由 Runtime 后绑定；未绑定时引擎
        # 按「无监督」模式工作（读失败仅记日志，与旧行为一致）。
        self._device_state: DeviceStatePort | None = None

        # 采集执行状态端口（可选）：Runtime 后绑定；未绑定时不做
        # collect 生命周期上报。
        self._acquisition_state: AcquisitionStatePort | None = None

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

    def attach_device_state(self, device_state: DeviceStatePort) -> None:
        """绑定设备连接状态端口——由 Runtime 在自身构造时调用一次。

        绑定后 :meth:`collect` 在读之前经 :meth:`DeviceStatePort.ensure_connected`
        确认设备可用（断线设备在此完成带节流的重连），读之后上报结果；
        协议实例的生命周期仍完全属于 Runtime。
        """
        self._device_state = device_state

    def attach_acquisition_state(self, acquisition_state: AcquisitionStatePort) -> None:
        """绑定采集执行状态端口——由 Runtime 在自身构造时调用一次。

        绑定后 :meth:`collect` 上报每次执行的开始/成功/失败（粒度
        ``(device, group)``）；订阅推送路径不是调度 Job 运行，不上报。
        """
        self._acquisition_state = acquisition_state

    def add_observer(self, callback: Callable[[list[PointValue]], None]) -> None:
        """注册采集观察者——每批处理完成后同步回调一次。

        观察者在事件循环上同步执行，异常被捕获并记录，不会干扰采集管线。
        """
        self._observers.append(callback)

    # ------------------------------------------------------------------
    # 采集执行
    # ------------------------------------------------------------------

    async def collect(self, device_id: str, group: str) -> None:
        """执行一次轮询采集：读设备该 group 的点 → 处理 → 路由 → 派发。

        这是调度 Job 的执行体入口。引擎根据当前点表找到该设备该 group 的
        所有点并批量读取（一个 ``(device, group)`` 对应一个调度 Job）。
        单次采集失败（设备不可达、驱动异常等）只记录 warning，不上抛——
        否则一次失败会让调度 Job 持续打堆栈甚至影响其他 Job；下个周期会
        自然重试，与旧设备循环的容错语义一致。

        Args:
            device_id: 目标设备。
            group: 轮询分组名——只采集 ``PointConfig.group`` 等于该值的点。

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
            if p.group == group
        ]
        if not refs:
            logger.debug("设备 '%s' 分组 '%s' 无点位——跳过采集", device_id, group)
            return

        acq = self._acquisition_state
        if acq is not None:
            acq.report_collect_started(device_id, group)
        failure: str | None = None
        partial = False
        batch: list[PointValue] | None = None
        try:
            if self._device_state is not None and not await self._device_state.ensure_connected(
                device_id
            ):
                # 断线且重连节流中：本次判定 FAILED（不重发 read），Job 保留，
                # 下一周期继续尝试。
                failure = "device disconnected (reconnect backoff)"
                logger.debug("设备 '%s' 断线且重连节流中——跳过本次采集", device_id)
            else:
                try:
                    if self._read_timeout is not None:
                        batch = await asyncio.wait_for(
                            proto.read(refs), timeout=self._read_timeout
                        )
                    else:
                        batch = await proto.read(refs)
                except TimeoutError as exc:
                    # 读超时：错误语义定位到 read 阶段。两种来源——外层
                    # ``asyncio.wait_for`` 兜底（read_timeout 已配置），或驱动
                    # 内部协议/socket 超时（read_timeout 未配置，沿用驱动消息）。
                    if self._read_timeout is not None:
                        failure = f"read timeout after {self._read_timeout:.1f}s"
                        logger.warning(
                            "read timeout: device=%s group=%s timeout=%.1fs",
                            device_id,
                            group,
                            self._read_timeout,
                        )
                    else:
                        failure = str(exc) or "read timeout"
                        logger.warning(
                            "read timeout: device=%s group=%s (driver-level)",
                            device_id,
                            group,
                        )
                    if self._device_state is not None:
                        self._device_state.report_read_failure(
                            device_id, TimeoutError("read timeout")
                        )
                except Exception as exc:
                    failure = str(exc) or type(exc).__name__
                    if self._device_state is not None:
                        self._device_state.report_read_failure(device_id, exc)
                    logger.warning(
                        "设备 '%s' 分组 '%s' 轮询失败——下个周期重试",
                        device_id,
                        group,
                        exc_info=True,
                    )
                else:
                    if self._device_state is not None:
                        self._device_state.report_read_success(device_id)
                    # 批量读允许部分失败（协议驱动以 quality=BAD 表达单点失败）：
                    # 全 GOOD → SUCCESS；GOOD+BAD 混合 → PARTIAL（不计连续失败）；
                    # 空批或全 BAD → 没有任何有效结果 → FAILED。BAD 批次照常进入
                    # 管线与派发（数据质量信息应流向 sink）。
                    bad = sum(1 for v in batch if v.quality == Quality.BAD)
                    if not batch or bad == len(batch):
                        failure = f"no valid values ({bad}/{len(batch)} BAD)"
                    else:
                        partial = bad > 0
            if batch is not None:
                await self.process_and_route(batch)
        except Exception as exc:
            # 管线/路由/派发阶段的意外异常：如实记为失败后原样上抛
            # （保持既有「非读异常传播给调度器记录」语义），running 归位。
            if acq is not None:
                acq.report_collect_failure(device_id, group, str(exc) or type(exc).__name__)
            raise
        if acq is not None:
            if failure is not None:
                acq.report_collect_failure(device_id, group, failure)
            else:
                acq.report_collect_success(device_id, group, partial=partial)

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
        # BAD 质量点单独计数（数据质量问题）——不计入 points_dropped
        # （那是 Sink 派发/背压丢弃语义）。
        bad = sum(1 for v in batch if v.quality == Quality.BAD)
        if bad:
            self._on_points_bad(bad)
        processed = await self._pipeline.process(batch)
        self._notify_observers(processed)
        routed = self._router.route(processed)
        if self._delivery is not None:
            # 投递策略过滤（interval/every_n/on_change）——Router 决定
            # 「发到哪些 sink」，DeliveryDispatcher 决定「这批是否投递」。
            routed = self._delivery.evaluate(routed)
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

    async def replace_delivery(self, new_delivery: DeliveryDispatcher | None) -> None:
        """原子替换投递策略（规则/投递配置变更时由 Runtime 调用）。

        只替换策略状态，不涉及 Sink/Protocol 重建（B.4）。
        """
        self._delivery = new_delivery
        logger.info("Delivery dispatcher replaced")

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
