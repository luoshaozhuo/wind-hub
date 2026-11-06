"""有界非阻塞日志边界：队列满仅丢弃普通观测日志，不影响业务线程。"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Sequence


class BoundedAsyncHandler(logging.Handler):
    """将根 Logger 的普通观测日志交给独占线程处理。"""

    def __init__(
        self,
        targets: Sequence[logging.Handler],
        *,
        capacity: int,
        process_name: str,
        on_drop: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        if capacity <= 0:
            raise ValueError("log queue capacity must be positive")
        if not targets:
            raise ValueError("logging requires an output handler")
        self._targets = tuple(targets)
        self._on_drop = on_drop
        self._queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._worker = threading.Thread(
            target=self._run, name=f"core-log-{process_name}", daemon=True
        )
        self._dropped = 0
        self._closed = False
        self._worker.start()

    @property
    def dropped(self) -> int:
        return self._dropped

    def emit(self, record: logging.LogRecord) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._dropped += 1
            if self._on_drop is not None:
                try:
                    self._on_drop()
                except Exception:
                    pass

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                record = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                for handler in self._targets:
                    try:
                        handler.handle(record)
                    except Exception:
                        # 观测出口故障不得打断工作线程或传播至业务 I/O。
                        continue
            finally:
                self._queue.task_done()

    def stop(self, *, timeout: float) -> bool:
        """返回是否完全收敛；失败时不提前关闭工作线程拥有的输出资源。"""
        self._stop.set()
        self._worker.join(timeout=timeout)
        if self._worker.is_alive():
            return False
        self._closed = True
        for handler in self._targets:
            try:
                handler.flush()
            finally:
                handler.close()
        super().close()
        return True
