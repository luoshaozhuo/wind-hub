"""Runtime 启动与优雅停机编排。

启动阶段只连接设备、打开 Sink、创建 Sink consumer，并展开默认 STOPPED 的
Task Instance；不会自动启动采集任务。设备或 Sink 单项启动失败被隔离，使其他
可用组件仍能运行。

停机阶段先关闭采集句柄，再结束 Sink consumer、flush/close Sink，最后关闭设备。
各组件关闭失败记录后继续，以最大化整体资源释放。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from wind_hub.application.runtime.task_instance import TaskInstanceState

if TYPE_CHECKING:
    from wind_hub.application.runtime.runtime import Runtime

logger = logging.getLogger(__name__)


def _is_connection_level(exc: BaseException) -> bool:
    """判断异常链是否属于常见连接级失败。

    Args:
        exc: 捕获到的异常。

    Returns:
        异常或其 cause 为 TimeoutError/ConnectionRefusedError/OSError 时返回 True。
    """
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError | ConnectionRefusedError | OSError):
            return True
        current = current.__cause__
    return False


class RuntimeLifecycle:
    """持有 Runtime 的启动/停机编排逻辑，避免 Runtime 主类继续膨胀。"""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def start(self) -> None:
        """启动 Runtime 基础资源。

        单设备/单 Sink 失败只记录状态并继续，避免一个现场端点阻断整进程。
        Task Instance 仅注册为 STOPPED，必须由控制面显式启动采集。
        """
        async with self._runtime._lifecycle_lock:
            if self._runtime._running:
                return
            self._runtime._running = True
            self._runtime._started = False

            # 点映射已在装配期（组合根 / add_device / rebuild_device）注入
            # 到各 Device 的协议实例，这里只做连接。
            for device_id, device in self._runtime._devices.items():
                try:
                    await asyncio.wait_for(
                        device.connect(),
                        timeout=self._runtime._config.connect_timeout,
                    )
                    self._runtime._state_for(device_id).mark_success(self._runtime._clock())
                    logger.info("Device '%s' connected", device_id)
                except TimeoutError:
                    self._runtime._note_connect_failure(
                        device_id,
                        TimeoutError("connect timeout"),
                    )
                    logger.warning(
                        "connect timeout: device=%s timeout=%.1fs — skipped",
                        device_id,
                        self._runtime._config.connect_timeout,
                    )
                except Exception as exc:
                    self._runtime._note_connect_failure(device_id, exc)
                    if _is_connection_level(exc):
                        logger.warning(
                            "Device '%s' failed to connect — skipped: %s",
                            device_id,
                            exc,
                        )
                    else:
                        logger.warning(
                            "Device '%s' failed to connect — skipped",
                            device_id,
                            exc_info=True,
                        )

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
        """按依赖逆序优雅停止 Runtime。

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

            # 只向真正存在 consumer 的 Sink queue 投递终止哨兵。启动失败的
            # Sink 没有 consumer；若其 queue 已满，对该 queue 执行 put 会使
            # 优雅停机永久阻塞。
            for name in self._runtime._sink_tasks:
                await self._runtime._queues[name].put([])

            for task in self._runtime._sink_tasks.values():
                try:
                    await asyncio.wait_for(
                        task,
                        timeout=self._runtime._config.shutdown_timeout,
                    )
                except TimeoutError:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
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

            for device_id, device in self._runtime._devices.items():
                try:
                    await device.close()
                except Exception:
                    logger.warning("Device '%s' close failed", device_id, exc_info=True)
