"""AcquisitionEngine——PointValue 数据流的统一处理入口（Domain）。

把一次获得的 PointValue 批次经过 Observer 和 targets 投递到 Sink，并维护
采集执行状态/统计。主动轮询（Modbus / ADS Sum）与订阅推送（ADS
notification / IEC104 spontaneous）最终都进入 :meth:`process`：

    主动轮询：ReadableDevice.read()  → engine.collect()（内含 process）
    订阅推送：协议 callback          → engine.process()

不负责：

- 「什么时候执行」——那是 Application Runtime 的 acquisition handle
  （fixed-rate polling / subscription）的职责；
- Protocol / Device 的创建、连接与关闭——那是 DeviceRuntime 的生命周期职责；
- Sink 队列、背压与消费者任务——经 :class:`SinkDispatchPort` 端口委托。

并发语义：不同 Task Instance 的 :meth:`collect` / :meth:`process` 可并发
执行；同一实例由其 acquisition handle 串行驱动，天然不重入。引擎自身
无锁，依赖各 Port 实现的并发安全。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from core.application import Quality

from .point_value import PointValue

logger = logging.getLogger(__name__)


class ReadableDevice(Protocol):
    """主动读取路径对运行时设备的结构化依赖（避免 Domain → Application 反向依赖）。

    引擎只依赖三件事：设备身份、按 point_group 选点、按 point_group 批量读。
    """

    @property
    def device_id(self) -> str:
        """设备标识（状态上报与日志用）。"""
        ...

    def point_ids(self, point_group: str) -> list[str]:
        """该 point_group 的点位集合；空表示本次无点可采。"""
        ...

    async def read(self, point_group: str) -> list[PointValue]:
        """批量读取该 point_group 的全部点位（工程值）。"""
        ...


class SinkDispatchPort(Protocol):
    """Sink 派发端口——把按 sink 分组的批次交给 Sink 侧（队列/背压/消费者）。"""

    async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
        """派发一批按 sink 名分组的点值；空批次由实现方忽略。"""
        ...


class DeviceStatePort(Protocol):
    """设备连接状态端口——采集前确保连接、采集后上报结果。

    引擎**不管理 Protocol 生命周期**（不重连、不计失败次数），只在「读
    之前问一句能否读、读之后如实报告结果」。``ensure_connected`` 返回
    ``False``（断线且重连节流中）时本次采集直接跳过——断线设备不再发起
    注定失败的 read，也不形成连接风暴。
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

    ``execution_id`` 是本次周期采集执行标识（Runtime 传 Task Instance
    ID）——同一个 ``(device, point_group)`` 可能被多个 Task 采集，状态
    必须按执行标识区分，不能按设备/分组合并。

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

    Args:
        on_points_collected: 采集点数计数回调（组合根接指标，Domain 不
            依赖 Infrastructure）。
        on_points_bad: BAD 质量点计数回调（数据质量问题，与背压丢弃分开
            计数）。
        read_timeout: 应用层读超时（外层兜底，秒）；协议驱动内部底层超时
            各自保留。``None`` 表示不加外层超时。
    """

    def __init__(
        self,
        on_points_collected: Callable[[int], None] | None = None,
        on_points_bad: Callable[[int], None] | None = None,
        read_timeout: float | None = None,
    ) -> None:
        self._on_points_collected = on_points_collected or (lambda _n: None)
        self._on_points_bad = on_points_bad or (lambda _n: None)
        self._read_timeout = read_timeout

        # 各端口由 Runtime 在装配完成后注入（engine 先于 runtime 创建）；
        # 未绑定 sink 派发前调用 collect/process 抛 RuntimeError——组合根
        # 保证绑定发生在任何采集触发之前。
        self._sink_dispatch: SinkDispatchPort | None = None
        self._device_state: DeviceStatePort | None = None
        self._acquisition_state: AcquisitionStatePort | None = None

        # 同步观察者：每批处理后的点值全量通知（如 IEC104 从站快照）。
        # 观察者异常被隔离，绝不影响采集链路。
        self._observers: list[Callable[[list[PointValue]], None]] = []

        self._points_collected = 0

    # ------------------------------------------------------------------
    # 装配缝（组合根 / Runtime 专用）
    # ------------------------------------------------------------------

    def attach_sink_dispatch(self, dispatch: SinkDispatchPort) -> None:
        """绑定 Sink 派发端口——由 Runtime 在自身构造时调用一次。"""
        self._sink_dispatch = dispatch

    def attach_device_state(self, device_state: DeviceStatePort) -> None:
        """绑定设备连接状态端口——由 Runtime 在自身构造时调用一次。"""
        self._device_state = device_state

    def attach_acquisition_state(self, acquisition_state: AcquisitionStatePort) -> None:
        """绑定采集执行状态端口——由 Runtime 在自身构造时调用一次。"""
        self._acquisition_state = acquisition_state

    def add_observer(self, callback: Callable[[list[PointValue]], None]) -> None:
        """注册采集观察者——每批处理完成后同步回调一次，异常隔离。"""
        self._observers.append(callback)

    # ------------------------------------------------------------------
    # 主动采集（薄封装：ReadableDevice.read → process）
    # ------------------------------------------------------------------

    async def collect(
        self,
        device: ReadableDevice,
        point_group: str,
        targets: list[str],
        execution_id: str,
    ) -> None:
        """执行一次主动轮询采集：读设备该 point_group 的点 → :meth:`process`。

        单次采集失败（设备不可达、驱动异常等）只记录 warning，不上抛——
        下个周期会自然重试。

        Raises:
            RuntimeError: Sink 派发端口尚未绑定（装配未完成）。
        """
        device_id = device.device_id
        point_ids = device.point_ids(point_group)
        if not point_ids:
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
                    # 读超时：错误语义定位到 read 阶段（外层 wait_for 兜底或
                    # 驱动内部协议/socket 超时）。
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
                    bad = sum(1 for v in batch if v.quality is Quality.BAD)
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

        订阅数据已经到达，不得再绕回 :meth:`collect`。同一个点被多个不同
        Task 采集时，各 Task 的 targets 独立派发——不做按 point_id 全局去重。

        Raises:
            RuntimeError: Sink 派发端口尚未绑定（装配未完成）。
        """
        if not batch:
            return
        if self._sink_dispatch is None:
            raise RuntimeError("AcquisitionEngine 未绑定 Sink 派发端口——组合根未完成装配")
        self._points_collected += len(batch)
        self._on_points_collected(len(batch))
        bad = sum(1 for v in batch if v.quality is Quality.BAD)
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


__all__ = [
    "AcquisitionEngine",
    "AcquisitionStatePort",
    "DeviceStatePort",
    "ReadableDevice",
    "SinkDispatchPort",
]
