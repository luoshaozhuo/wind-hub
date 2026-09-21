"""Lifecycle orchestration for Runtime."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from wind_hub.application.runtime.task_instance import TaskInstanceState

if TYPE_CHECKING:
    from wind_hub.application.runtime.runtime import Runtime

logger = logging.getLogger(__name__)


def _is_connection_level(exc: BaseException) -> bool:
    """Determine whether the exception represents a transient connection problem."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError | ConnectionRefusedError | OSError):
            return True
        current = current.__cause__
    return False


class RuntimeLifecycle:
    """Lifecycle orchestration for the runtime startup and shutdown phases."""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def start(self) -> None:
        """Start the runtime and bootstrap all runtime dependencies."""
        async with self._runtime._lifecycle_lock:
            if self._runtime._running:
                return
            self._runtime._running = True
            self._runtime._started = False

            for device_id, proto in self._runtime._protocols.items():
                proto.set_points_mapping(self._runtime._points_by_device.get(device_id, []))

            for device_id, proto in self._runtime._protocols.items():
                try:
                    await asyncio.wait_for(
                        proto.connect(),
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
            # 周期采集，只有 CLI / Web API 的显式 start 才创建实例采集协程。
            await self._runtime._sync_task_instances()

            self._runtime._started = True

    async def stop(self) -> None:
        """Shut down the runtime gracefully."""
        async with self._runtime._lifecycle_lock:
            if not self._runtime._running:
                return
            self._runtime._running = False
            self._runtime._started = False

            # 停全部实例采集协程——逐个取消并等待结束，不留 orphan task。
            for instance_id in list(self._runtime._task_coroutines):
                await self._runtime._cancel_instance_coroutine(instance_id)
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

            for device_id, proto in self._runtime._protocols.items():
                try:
                    await proto.close()
                except Exception:
                    logger.warning("Device '%s' close failed", device_id, exc_info=True)
