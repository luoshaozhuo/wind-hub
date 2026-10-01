"""AcquisitionEngine —— PointValue 数据流的统一处理入口。

架构位置：domain 层。职责一句话：

    把一次获得的 PointValue 批次经过 Observer 和 targets 投递到 Sink
    并维护采集执行状态/统计；对于主动采集，也提供
    ``Device.read → process`` 的薄封装（:meth:`collect`）。

数据在 ``PointValue[]`` 这一层汇合——主动轮询（Modbus / ADS Sum）与
订阅推送（ADS notification / IEC104 spontaneous）最终都进入
:meth:`process`：

    主动轮询：Device.read()        → engine.collect()（内含 process）
    订阅推送：协议 callback        → engine.process()

不负责：

- 「什么时候执行」——那是 application/runtime 的 acquisition handle
  （fixed-rate polling / subscription）的职责；
- Protocol / Device 的创建、连接与关闭——那是 Runtime 的生命周期职责；
- Sink 队列、背压与消费者任务——经 :class:`SinkDispatchPort` 端口委托给
  实现方（Runtime）。

并发语义：不同 Task Instance 的 :meth:`collect` / :meth:`process` 可并发
执行；同一实例由其 acquisition handle 串行驱动，天然不重入。本引擎自身
无锁，依赖各 Port 实现的并发安全。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from wind_hub.domain.model.point import PointRef, PointValue, Quality

logger = logging.getLogger(__name__)


class ReadableDevice(Protocol):
    """主动读取路径对运行时设备的结构化依赖（避免 domain → application 反向依赖）。

    由 application/runtime 的 ``Device`` 结构化满足；引擎只依赖这三件
    事：设备身份、按 point_group 选点、按 point_group 批量读。
    """

    @property
    def device_id(self) -> str:
        """设备标识（状态上报与日志用）。"""
        ...

    def point_refs(self, point_group: str) -> list[PointRef]:
        """该 point_group 的批量读寻址引用。"""
        ...

    async def read(self, point_group: str) -> list[PointValue]:
        """批量读取该 point_group 的全部点位。"""
        ...


class SinkDispatchPort(Protocol):
    """Sink 派发端口——把按 sink 分组的批次交给 Sink 侧（队列/背压/消费者）。

    由 Runtime 实现：引擎不感知队列与背压策略，只保证「发到哪个 sink、
    批次内容是什么」如实传递。派发过程中的丢弃/阻塞语义由实现方定义。
    """

    async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
        """派发一批按 sink 名分组的点值。

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

    由 Runtime 实现（持有 ``{execution_id: AcquisitionRuntimeState}``）。
    与 :class:`DeviceStatePort` 分维度：本端口描述**业务执行**（这个采集
    执行最近跑得怎样），不描述设备连接。

    ``execution_id`` 是调用方提供的本次周期采集执行标识（Runtime 传
    Task Instance 的 ``instance_id``）——同一个 ``(device, point_group)``
    可能被多个 Task 采集，状态必须按执行标识区分，不能按设备/分组合并。

    一次 collect 的判定口径：

    - ``success``——批量读无异常且至少一个点有效（``partial=True`` 表示
      批次为 GOOD/BAD 混合，仍算成功、不计连续失败）；
    - ``failure``——读整体抛异常、读超时、断线跳过（不重发 read）、
      或批次没有任何有效结果（全 BAD / 空批）。
    """

    def report_collect_started(self, execution_id: str, device_id: str, group: str) -> None:
        """一次 collect 开始（running=True）。"""
        ...

    def report_collect_success(
        self, execution_id: str, device_id: str, group: str, *, partial: bool
    ) -> None:
        """一次 collect 成功结束；``partial`` 表示批次含 BAD 点的混合结果。"""
        ...

    def report_collect_failure(
        self, execution_id: str, device_id: str, group: str, error: str
    ) -> None:
        """一次 collect 失败结束（描述已压成一行文本）。"""
        ...


class AcquisitionEngine:
    """采集引擎——PointValue 批次的统一处理入口。

    注入依赖：

    - ``on_points_collected`` / ``on_points_bad`` — 采集/BAD 点计数回调
      （组合根接 Prometheus 计数器，domain 不依赖 infra）；
    - ``read_timeout`` — 应用层读超时（外层兜底）；协议驱动内部的底层
      超时各自保留，``None`` 表示不加外层超时。

    引擎**不持有**协议注册表、设备索引或点表——设备以
    :class:`ReadableDevice` 结构化参数传入；Task 时序（interval /
    调度）完全属于 application/runtime 的 acquisition handle。

    运行期统计：``points_collected`` 在 :meth:`process` 入口统一计数；
    路由/丢弃计数属于 Sink 派发侧（Runtime）。
    """

    def __init__(
        self,
        on_points_collected: Callable[[int], None] | None = None,
        on_points_bad: Callable[[int], None] | None = None,
        read_timeout: float | None = None,
    ) -> None:
        self._on_points_collected = on_points_collected or (lambda _n: None)
        # BAD 质量点计数回调（协议采集结果中的 Quality.BAD——数据质量问题，
        # 与背压丢弃 points_dropped 语义不同，分开计数）。
        self._on_points_bad = on_points_bad or (lambda _n: None)
        # 应用层读超时（外层兜底）：一次批量读允许占用的最大时间；协议驱动
        # 内部的底层 socket/协议超时各自保留，两层职责不同。``None`` 表示
        # 不加外层超时（组合根总是注入配置值）。
        self._read_timeout = read_timeout

        # Sink 派发端口由 Runtime 在装配完成后注入（engine 先于 runtime 创建，
        # 无法构造期传入）；未绑定前调用 collect/process 会抛
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

        绑定后 :meth:`collect` 上报每次执行的开始/成功/失败（以上传入的
        ``execution_id`` 为粒度）。
        """
        self._acquisition_state = acquisition_state

    def add_observer(self, callback: Callable[[list[PointValue]], None]) -> None:
        """注册采集观察者——每批处理完成后同步回调一次。

        观察者在事件循环上同步执行，异常被捕获并记录，不会干扰采集链路。
        """
        self._observers.append(callback)

    # ------------------------------------------------------------------
    # 主动采集（薄封装：Device.read → process）
    # ------------------------------------------------------------------

    async def collect(
        self,
        device: ReadableDevice,
        point_group: str,
        targets: list[str],
        execution_id: str,
    ) -> None:
        """执行一次主动轮询采集：读设备该 point_group 的点 → :meth:`process`。

        这是 POLL 型 acquisition handle 每 tick 调用的执行体。单次采集
        失败（设备不可达、驱动异常等）只记录 warning，不上抛——下个
        周期会自然重试。

        Args:
            device: 目标运行时设备（持有配置、点表与协议实例）。
            point_group: 点位分组——采集 ``point_groups`` 含该值的点。
            targets: 输出目标 Sink 名列表（来自 Task Instance）。
            execution_id: 本次周期采集的执行标识（Task Instance ID）——
                采集状态上报按此键区分，允许同一 ``(device, point_group)``
                被多个 Task 采集。

        Raises:
            RuntimeError: Sink 派发端口尚未绑定（装配未完成）。
        """
        device_id = device.device_id
        refs = device.point_refs(point_group)
        if not refs:
            logger.debug("设备 '%s' 分组 '%s' 无点位——跳过采集", device_id, point_group)
            return

        acq = self._acquisition_state
        if acq is not None:
            acq.report_collect_started(execution_id, device_id, point_group)
        failure: str | None = None
        partial = False
        batch: list[PointValue] | None = None
        try:
            if self._device_state is not None and not await self._device_state.ensure_connected(
                device_id
            ):
                # 断线且重连节流中：本次判定 FAILED（不重发 read），实例保留，
                # 下一周期继续尝试。
                failure = "device disconnected (reconnect backoff)"
                logger.debug("设备 '%s' 断线且重连节流中——跳过本次采集", device_id)
            else:
                try:
                    if self._read_timeout is not None:
                        batch = await asyncio.wait_for(
                            device.read(point_group), timeout=self._read_timeout
                        )
                    else:
                        batch = await device.read(point_group)
                except TimeoutError as exc:
                    # 读超时：错误语义定位到 read 阶段。两种来源——外层
                    # ``asyncio.wait_for`` 兜底（read_timeout 已配置），或驱动
                    # 内部协议/socket 超时（read_timeout 未配置，沿用驱动消息）。
                    if self._read_timeout is not None:
                        failure = f"read timeout after {self._read_timeout:.1f}s"
                        logger.warning(
                            "read timeout: device=%s group=%s timeout=%.1fs",
                            device_id,
                            point_group,
                            self._read_timeout,
                        )
                    else:
                        failure = str(exc) or "read timeout"
                        logger.warning(
                            "read timeout: device=%s group=%s (driver-level)",
                            device_id,
                            point_group,
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
                        point_group,
                        exc_info=True,
                    )
                else:
                    if self._device_state is not None:
                        self._device_state.report_read_success(device_id)
                    # 批量读允许部分失败（协议驱动以 quality=BAD 表达单点失败）：
                    # 全 GOOD → SUCCESS；GOOD+BAD 混合 → PARTIAL（不计连续失败）；
                    # 空批或全 BAD → 没有任何有效结果 → FAILED。BAD 批次照常
                    # 派发（数据质量信息应流向 sink）。
                    bad = sum(1 for v in batch if v.quality == Quality.BAD)
                    if not batch or bad == len(batch):
                        failure = f"no valid values ({bad}/{len(batch)} BAD)"
                    else:
                        partial = bad > 0
            if batch is not None:
                await self.process(batch, targets)
        except Exception as exc:
            # 派发阶段的意外异常：如实记为失败后原样上抛（由调用方——
            # polling handle——记录并继续下一周期），running 归位。
            if acq is not None:
                acq.report_collect_failure(
                    execution_id, device_id, point_group, str(exc) or type(exc).__name__
                )
            raise
        if acq is not None:
            if failure is not None:
                acq.report_collect_failure(execution_id, device_id, point_group, failure)
            else:
                acq.report_collect_success(execution_id, device_id, point_group, partial=partial)

    # ------------------------------------------------------------------
    # 统一数据处理入口（主动轮询与订阅推送在此汇合）
    # ------------------------------------------------------------------

    async def process(self, batch: list[PointValue], targets: list[str]) -> None:
        """统计 → Observers → 按 targets 原样派发到 Sink。

        主动轮询（:meth:`collect`）与订阅推送（ADS notification /
        IEC104 spontaneous 的协议回调）都经本入口处理数据——订阅数据
        已经到达，不得再绕回 :meth:`collect`。

        采集统计在本方法入口统一计数。同一个点被多个不同 Task 采集时，各
        Task 的 targets 独立派发——不做按 ``point_id`` 的全局去重。

        Raises:
            RuntimeError: Sink 派发端口尚未绑定（装配未完成）。
        """
        if not batch:
            return
        if self._sink_dispatch is None:
            raise RuntimeError("AcquisitionEngine 未绑定 Sink 派发端口——组合根未完成装配")
        self._points_collected += len(batch)
        self._on_points_collected(len(batch))
        # BAD 质量点单独计数（数据质量问题）——不计入 points_dropped
        # （那是 Sink 派发/背压丢弃语义）。
        bad = sum(1 for v in batch if v.quality == Quality.BAD)
        if bad:
            self._on_points_bad(bad)
        self._notify_observers(batch)
        await self._sink_dispatch.dispatch({sink: batch for sink in targets})

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def points_collected(self) -> int:
        """累计采集点数——进入引擎的点值总数（单调不减）。"""
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
