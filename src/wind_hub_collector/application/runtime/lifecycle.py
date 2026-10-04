"""CollectorRuntime 启动与优雅停机编排。

启动阶段只连接设备、打开 Sink、创建 Sink consumer，并展开默认 STOPPED 的
Task Instance；不会自动启动采集任务。设备或 Sink 单项启动失败被隔离，使其他
可用组件仍能运行。

停机阶段先关闭采集句柄，再结束 Sink consumer、flush/close Sink，最后关闭设备。
各组件关闭失败记录后继续，以最大化整体资源释放。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from wind_hub_collector.application.runtime.task_instance import TaskInstanceState

if TYPE_CHECKING:
    from wind_hub_collector.application.runtime.runtime import CollectorRuntime

logger = logging.getLogger(__name__)


class RuntimeLifecycle:
    """持有 CollectorRuntime 的启动/停机编排逻辑，避免 CollectorRuntime 主类继续膨胀。"""

    def __init__(self, runtime: CollectorRuntime) -> None:
        self._runtime = runtime

    async def start(self) -> None:
        """启动 CollectorRuntime 基础资源。

        单设备/单 Sink 失败只记录状态并继续，避免一个现场端点阻断整进程。
        Task Instance 仅注册为 STOPPED，必须由控制面显式启动采集。
        """
        async with self._runtime._lifecycle_lock:
            if self._runtime._running:
                return
            self._runtime._running = True
            self._runtime._started = False

            await self._runtime.device_runtime.connect_all()

            for name, sink in self._runtime._sinks.items():
                try:
                    await sink.open()
                    logger.info("Sink '%s' opened", name)
                except Exception:
                    logger.warning("Sink '%s' failed to open — skipped", name, exc_info=True)
                    self._runtime._unhealthy_sinks.add(name)

            for name, sink in self._runtime._sinks.items():
                if name in self._runtime._unhealthy_sinks:
                    continue
                task = asyncio.create_task(self._runtime._sink_consumer(name, sink))
                self._runtime._sink_tasks[name] = task

            # 注册采集 Task Instance（默认 STOPPED）——程序启动不自动开始
            # 采集，只有 gRPC 控制面的显式 start 才启动 acquisition。
            await self._runtime._sync_task_instances()

            self._runtime._started = True

    async def stop(self) -> None:
        """按依赖逆序优雅停止 CollectorRuntime。

        采集句柄先停，随后终止 Sink consumer 并 flush/close，最后关闭设备连接。
        单资源清理异常被记录但不阻断其他资源释放。
        """
        async with self._runtime._lifecycle_lock:
            if not self._runtime._running:
                return
            self._runtime._running = False
            self._runtime._started = False

            # 关闭全部实例采集句柄——polling 协程取消、订阅注销，不留
            # 避免遗留后台 task 或订阅。
            for instance_id in list(self._runtime._acquisition_handles):
                await self._runtime._close_acquisition_handle(instance_id)
            # 停机后实例定义保留，统一标记 STOPPED——重启后需显式 start，
            # 与启动语义一致。
            for instance_id in self._runtime._instance_states:
                self._runtime._instance_states[instance_id] = TaskInstanceState.STOPPED

            for queue in self._runtime._queues.values():
                await queue.put([])
            for task in self._runtime._sink_tasks.values():
                try:
                    await asyncio.wait_for(
                        task,
                        timeout=self._runtime._config.shutdown_timeout,
                    )
                except TimeoutError:
                    task.cancel()
                except asyncio.CancelledError:
                    pass
            self._runtime._sink_tasks.clear()

            for name, sink in self._runtime._sinks.items():
                try:
                    await sink.flush()
                except Exception:
                    logger.warning("Sink '%s' flush failed", name, exc_info=True)
                try:
                    await sink.close()
                except Exception:
                    logger.warning("Sink '%s' close failed", name, exc_info=True)

            await self._runtime.device_runtime.close_all()
