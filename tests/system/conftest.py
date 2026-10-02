"""System E2E 共享 fixture——把 subprocess harness 组装成测试可直接消费的形态。

约定：

- 每个测试的 Collector 使用独立配置目录（``tmp_path``）与独立 gRPC 端口，
  互不共享进程与状态；
- fixture 结束一律走 :meth:`CollectorProcess.terminate` 优雅停机；
  已异常退出的进程跳过 terminate，只回收日志；
- 协议 server fixture 复用 ``tests/fixtures/servers`` 的真实协议栈实现。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from tests.system.process import CollectorProcess, start_collector
from tests.system.wait import wait_grpc_ready


@pytest.fixture
def collector_factory(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Awaitable[CollectorProcess]]]:
    """Collector 进程工厂：``await factory(config_dir)`` 返回 gRPC 已就绪的进程。

    测试结束后统一优雅停机并回收日志；启动阶段失败（含 gRPC 不就绪）
    立即强制清理，不残留孤儿进程。
    """
    started: list[CollectorProcess] = []

    async def _start(config_dir: Path, **kwargs: object) -> CollectorProcess:
        proc = start_collector(config_dir, log_dir=tmp_path, **kwargs)  # type: ignore[arg-type]
        started.append(proc)
        try:
            await wait_grpc_ready(proc.grpc_target)
        except BaseException:
            proc.kill_tree()
            proc.close_log()
            started.remove(proc)
            raise
        return proc

    yield _start

    for proc in started:
        try:
            if proc.is_running():
                proc.terminate()
        except BaseException:
            proc.kill_tree()
        finally:
            proc.close_log()
