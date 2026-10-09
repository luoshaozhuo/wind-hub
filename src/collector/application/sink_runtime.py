"""Collector Sink 生命周期与交付的唯一权威。

只管理 Sink 子系统：Sink 实例注册表、per-sink 有界 queue、消费者
asyncio task、open 失败簿记（unhealthy）、背压/派发计数与
add/remove/rebuild 全部由本对象持有；同时实现 Domain 的
``SinkDispatchPort``，采集引擎的派发落点直接绑定本对象。

不负责：设备会话（DeviceRuntime）、Task/采集状态（TaskRuntime）、
Sink 实例从配置的创建（组合根注入的工厂）、ConfigDiff 计算
（CollectorConfigService）与跨子系统的启停顺序（CollectorRuntime）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from types import MappingProxyType

from core.application import ConnectionHealth

from ..domain.point_value import PointValue
from .config import RuntimeParams
from .reload import SinkDiff
from .sink_port import ExclusiveOpenSinkPort, SinkFactory, SinkPort
from .sinks import ResolvedSinkConfig

logger = logging.getLogger(__name__)


class SinkRuntime:
    """持有 Sink 注册表、queue、消费者任务与派发状态，并负责 Sink 热更新。

    接管传入的 Sink 注册表；不保存设备、Task 或采集句柄状态。
    ``_running`` 只决定 add/rebuild 时是否立即补建消费者——由
    :meth:`start` / :meth:`stop` 维护，与 CollectorRuntime 的就绪标志
    分属两层。

    失败语义：open 失败记录为 unhealthy 并跳过消费者，其余 Sink 照常
    运行；flush/close/write 失败只记日志不阻断。
    """

    def __init__(
        self,
        sinks: dict[str, SinkPort],
        params: RuntimeParams,
        sink_factory: SinkFactory | None = None,
    ) -> None:
        self._sinks = sinks
        self._params = params
        self._sink_factory = sink_factory
        # 每个 Sink 使用独立有界 queue，容量来自 RuntimeParams。
        self._queues: dict[str, asyncio.Queue[list[PointValue]]] = {
            name: asyncio.Queue(maxsize=params.queue_maxsize) for name in sinks
        }
        # Sink 消费者任务簿记。
        self._consumer_tasks: dict[str, asyncio.Task[None]] = {}
        # start() 阶段 open 失败的 Sink 不启动 consumer，并由 health() 持续暴露为 unhealthy。
        self._unhealthy: set[str] = set()
        # 运行期统计：派发/丢弃在派发路径（dispatch）计数。
        self._points_routed = 0
        self._points_dropped = 0
        self._running = False

    # ------------------------------------------------------------------
    # 只读视图（CollectorRuntime 聚合 / 查询服务经此读取）
    # ------------------------------------------------------------------

    @property
    def sinks(self) -> Mapping[str, SinkPort]:
        """当前 Sink 注册表的只读视图（随热重载就地反映最新内容）。

        本对象是注册表的唯一 owner——外部只能观察，生命周期变更必须经
        本对象的方法执行。
        """
        return MappingProxyType(self._sinks)

    def update_params(self, params: RuntimeParams) -> None:
        """热更新运行时参数快照。

        ``backpressure_policy`` 由本对象每次派发动态读取；
        ``shutdown_timeout`` 仅影响后续 stop/remove；``queue_maxsize`` 为
        restart-required（既有 queue 容量不随之变化）。
        """
        self._params = params

    def health(self) -> dict[str, ConnectionHealth]:
        """各 Sink 健康状态——open 失败的 Sink 以 unhealthy 覆盖其实现自报值。"""
        result: dict[str, ConnectionHealth] = {}
        for name, sink in self._sinks.items():
            if name in self._unhealthy:
                result[name] = ConnectionHealth(healthy=False, message="open failed")
            else:
                result[name] = sink.health()
        return result

    def queue_depths(self) -> dict[str, int]:
        """各 sink 队列当前深度（队列归 SinkRuntime 所有，SinkPort 自身不感知队列）。"""
        return {name: queue.qsize() for name, queue in self._queues.items()}

    @property
    def points_routed(self) -> int:
        """累计派发点数——成功进入 sink 队列的点值总数（单调不减）。"""
        return self._points_routed

    @property
    def points_dropped(self) -> int:
        """累计丢弃点数——背压策略丢弃的点值总数（单调不减）。"""
        return self._points_dropped

    # ------------------------------------------------------------------
    # Sink 派发端口实现（AcquisitionEngine → SinkRuntime 的落点）
    # ------------------------------------------------------------------

    async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
        """把按 sink 分组的批次入队，应用背压策略（实现 ``SinkDispatchPort``）。

        drop_new/drop_old 显式累计丢弃点数；block 通过 await queue.put()
        向采集任务施加背压。
        """
        for sink_name, batch in routed.items():
            if not batch:
                continue
            queue = self._queues.get(sink_name)
            if queue is None:
                continue
            await self._enqueue(queue, batch, sink_name)

    async def _enqueue(
        self,
        queue: asyncio.Queue[list[PointValue]],
        batch: list[PointValue],
        sink_name: str,
    ) -> None:
        """按当前背压策略把一个批次写入指定 sink 队列。"""
        policy = self._params.backpressure_policy

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
    # 生命周期（启停顺序由 CollectorRuntime 协调）
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """打开全部 Sink 并为健康实例创建消费者。

        单 Sink open 失败只标记 unhealthy 并继续，避免一个输出端点阻断
        整体启动；失败 Sink 不建消费者，由 :meth:`health` 持续暴露。
        """
        self._running = True
        for name, sink in self._sinks.items():
            try:
                await sink.open()
                logger.info("Sink '%s' opened", name)
            except Exception:
                logger.warning("Sink '%s' failed to open — skipped", name, exc_info=True)
                self._unhealthy.add(name)

        for name, sink in self._sinks.items():
            if name in self._unhealthy:
                continue
            task = asyncio.create_task(self._consumer(name, sink))
            self._consumer_tasks[name] = task

    async def stop(self) -> None:
        """排空队列、结束消费者并 flush/close 全部 Sink。

        先向每个队列投递空批次哨兵，让消费者处理完已入队数据后自行退出；
        单资源清理异常被记录但不阻断其他资源释放。调用方（CollectorRuntime）
        保证先停止采集，本方法返回后不再有新的派发入队。
        """
        self._running = False
        for queue in self._queues.values():
            await queue.put([])
        for task in self._consumer_tasks.values():
            try:
                await asyncio.wait_for(task, timeout=self._params.shutdown_timeout)
            except TimeoutError:
                task.cancel()
            except asyncio.CancelledError:
                pass
        self._consumer_tasks.clear()

        for name, sink in self._sinks.items():
            try:
                await sink.flush()
            except Exception:
                logger.warning("Sink '%s' flush failed", name, exc_info=True)
            try:
                await sink.close()
            except Exception:
                logger.warning("Sink '%s' close failed", name, exc_info=True)

    # ------------------------------------------------------------------
    # 热重载——sink 增删重建
    # ------------------------------------------------------------------

    async def add_sink(self, sink_name: str, cfg: ResolvedSinkConfig, sink: SinkPort) -> None:
        """运行时新增 sink；open 成功后才提交到注册表。

        部分 reload 失败重试时，若同名 sink 已存在，直接按 rebuild 路径
        收敛到目标实例，避免重复注册消费者或遗留半初始化对象。

        Raises:
            Exception: ``sink.open()`` 失败原样上抛；失败前不修改注册表。
        """
        if sink_name in self._sinks:
            await self.rebuild_sink(sink_name, cfg, sink)
            return

        await sink.open()
        queue: asyncio.Queue[list[PointValue]] = asyncio.Queue(maxsize=self._params.queue_maxsize)
        self._sinks[sink_name] = sink
        self._queues[sink_name] = queue
        logger.info("Hot-reload: sink '%s' opened", sink_name)

        if self._running:
            task = asyncio.create_task(self._consumer(sink_name, sink))
            self._consumer_tasks[sink_name] = task

    async def remove_sink(self, sink_name: str) -> None:
        """运行时移除 sink——停消费者、flush、关闭、移除队列。"""
        await self._stop_consumer(sink_name)

        old_sink = self._sinks.pop(sink_name, None)
        if old_sink is not None:
            await self._flush_and_close(sink_name, old_sink, "during removal")

        self._queues.pop(sink_name, None)
        self._unhealthy.discard(sink_name)
        logger.info("Hot-reload: sink '%s' removed", sink_name)

    async def rebuild_sink(
        self,
        sink_name: str,
        new_cfg: ResolvedSinkConfig,
        new_sink: SinkPort,
    ) -> None:
        """重建 Sink，并按资源能力选择 open-first / close-first。

        普通客户端型 Sink 先打开新实例再切换；实现 ExclusiveOpenSinkPort
        且 exclusive_open=True 的监听型 Sink 先关闭旧实例释放独占资源。
        若新实例 open 失败，会尽力重新打开旧实例。
        """
        del new_cfg
        old_sink = self._sinks.get(sink_name)
        exclusive = isinstance(new_sink, ExclusiveOpenSinkPort) and new_sink.exclusive_open
        if not exclusive:
            await new_sink.open()
            await self._replace_opened_sink(sink_name, old_sink, new_sink)
            return

        await self._stop_consumer(sink_name)
        if old_sink is not None:
            await self._flush_and_close(sink_name, old_sink, "during exclusive rebuild")

        try:
            await new_sink.open()
        except Exception:
            if old_sink is not None:
                try:
                    await old_sink.open()
                except Exception:
                    logger.error(
                        "Hot-reload: sink %s failed to restore old instance",
                        sink_name,
                        exc_info=True,
                    )
                    self._unhealthy.add(sink_name)
                else:
                    self._sinks[sink_name] = old_sink
                    self._restart_consumer(sink_name, old_sink)
            raise

        self._sinks[sink_name] = new_sink
        self._unhealthy.discard(sink_name)
        self._restart_consumer(sink_name, new_sink)
        logger.info("Hot-reload: exclusive sink %s re-opened", sink_name)

    async def apply_diff(
        self,
        diff: SinkDiff,
        new_sinks: Mapping[str, ResolvedSinkConfig],
    ) -> None:
        """按 diff 让运行态 Sink 注册表收敛到 enabled Sink 集合。

        新增/重建的 Sink 实例由组合根注入的工厂创建；工厂缺失时在任何
        删除动作之前失败，保持原有失败边界。
        """
        factory = self._sink_factory
        needs_factory = any(
            new_sinks[name].enabled for name in (*diff.added, *diff.updated) if name in new_sinks
        )
        if factory is None and needs_factory:
            raise RuntimeError("sink factory is not wired into Runtime")

        for name in diff.removed:
            await self.remove_sink(name)

        for name in diff.added:
            cfg = new_sinks[name]
            if not cfg.enabled:
                continue
            assert factory is not None
            await self.add_sink(name, cfg, factory(cfg))

        for name in diff.updated:
            cfg = new_sinks[name]
            if not cfg.enabled:
                await self.remove_sink(name)
                continue
            assert factory is not None
            sink = factory(cfg)
            if name in self._sinks:
                await self.rebuild_sink(name, cfg, sink)
            else:
                await self.add_sink(name, cfg, sink)

    # ------------------------------------------------------------------
    # 私有——消费者与重建辅助
    # ------------------------------------------------------------------

    async def _replace_opened_sink(
        self,
        sink_name: str,
        old_sink: SinkPort | None,
        new_sink: SinkPort,
    ) -> None:
        """切换一个已经成功 open 的普通 Sink。"""
        await self._stop_consumer(sink_name)
        if old_sink is not None:
            await self._flush_and_close(sink_name, old_sink, "during rebuild")
        self._sinks[sink_name] = new_sink
        self._unhealthy.discard(sink_name)
        self._restart_consumer(sink_name, new_sink)
        logger.info("Hot-reload: sink %s re-opened", sink_name)

    async def _stop_consumer(self, sink_name: str) -> None:
        """取消并等待单个消费者（幂等）；等待超时按原语义吞掉。"""
        task = self._consumer_tasks.pop(sink_name, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=self._params.shutdown_timeout)

    async def _flush_and_close(
        self,
        sink_name: str,
        sink: SinkPort,
        context: str,
    ) -> None:
        """先 flush 后 close；失败只记日志不阻断，保证释放路径走完整。"""
        try:
            await sink.flush()
        except Exception:
            logger.warning(
                "Hot-reload: sink %s flush failed %s",
                sink_name,
                context,
                exc_info=True,
            )
        try:
            await sink.close()
        except Exception:
            logger.warning(
                "Hot-reload: sink %s close failed %s",
                sink_name,
                context,
                exc_info=True,
            )

    def _restart_consumer(self, sink_name: str, sink: SinkPort) -> None:
        """运行中为重建后的 Sink 补建消费者；停机/未启动时不建。"""
        if not self._running:
            return
        self._consumer_tasks[sink_name] = asyncio.create_task(self._consumer(sink_name, sink))

    async def _consumer(self, sink_name: str, sink: SinkPort) -> None:
        """Per-sink 消费者任务——从队列取批次并写入 SinkPort。

        单次 write 失败只记日志、继续消费后续批次（不标记 unhealthy，
        健康状态由 SinkPort 自报）；空批次为停机哨兵。
        """
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
