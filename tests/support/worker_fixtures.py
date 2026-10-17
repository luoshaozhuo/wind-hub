"""Collector / Commander 子进程工厂 pytest fixture（system 与 integration 共享）。

生命周期语义：

- ``await factory(config_dir)`` 返回 gRPC 已就绪的进程；
- 测试结束统一优雅停机并回收日志；启动阶段失败（含 gRPC 不就绪）
  立即强制清理，不残留孤儿进程。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.process import CollectorProcess, start_collector, start_commander
from tests.support.wait import wait_commander_ready, wait_grpc_ready


@pytest.fixture
def collector_factory(
    tmp_path: Path,
) -> Iterator:
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


@pytest.fixture
def commander_factory(
    tmp_path: Path,
) -> Iterator:
    started: list[CollectorProcess] = []

    async def _start(config_dir: Path, **kwargs: object) -> CollectorProcess:
        proc = start_commander(config_dir, log_dir=tmp_path, **kwargs)  # type: ignore[arg-type]
        started.append(proc)
        try:
            await wait_commander_ready(proc.grpc_target)
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
