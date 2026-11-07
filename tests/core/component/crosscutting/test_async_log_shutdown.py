"""有界异步日志线程的并发关闭与超时所有权。"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.infrastructure.crosscutting.async_log import BoundedAsyncHandler

pytestmark = [pytest.mark.component, pytest.mark.reliability, pytest.mark.concurrency]


class _CountingTarget(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0
        self.close_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        pass

    def flush(self) -> None:
        self.flush_count += 1

    def close(self) -> None:
        self.close_count += 1
        super().close()


def test_concurrent_stop_closes_targets_once() -> None:
    target = _CountingTarget()
    handler = BoundedAsyncHandler((target,), capacity=8, process_name="concurrent-stop")
    with ThreadPoolExecutor(max_workers=8) as executor:
        settled = list(executor.map(lambda _: handler.stop(timeout=2.0), range(12)))
    assert all(settled)
    assert target.flush_count == 1
    assert target.close_count == 1


def test_timeout_preserves_target_until_worker_finishes() -> None:
    entered = threading.Event()
    release = threading.Event()

    class _BlockingTarget(_CountingTarget):
        def emit(self, record: logging.LogRecord) -> None:
            entered.set()
            assert release.wait(timeout=5)

    target = _BlockingTarget()
    handler = BoundedAsyncHandler((target,), capacity=8, process_name="blocking-stop")
    record = logging.LogRecord("source", logging.INFO, __file__, 1, "data", (), None)
    handler.emit(record)
    assert entered.wait(timeout=2)
    try:
        assert not handler.stop(timeout=0.01)
        assert target.close_count == 0
    finally:
        release.set()
    assert handler.stop(timeout=2.0)
    assert target.flush_count == 1
    assert target.close_count == 1
