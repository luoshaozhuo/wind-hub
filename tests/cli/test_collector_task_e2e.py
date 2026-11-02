"""UC-07 控制 Collector 采集任务 System Test（真实子进程 + 真 mTLS）。"""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from contracts import collector_pb2 as col
from contracts import common_pb2

pytestmark = [pytest.mark.system, pytest.mark.contract]


def _p(server: Any, method: str) -> str:
    return server.method_path("CollectorService", method)


def test_start_stop_task_with_revision_prefetch_and_confirm(cli_env: Any) -> None:
    """S01：start/stop 自动 GetTask 取 revision，确认门，乐观锁 expected_revision 上送。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "GetTask"),
        col.GetTaskResponse(task=col.Task(task_id="t1", revision="rev-9")),
    )
    server.respond_value(
        _p(server, "StartTask"),
        common_pb2.OperationResult(accepted=True, actual_state="STARTED"),
    )
    result = cli_env.subprocess_invoke("collector", "start", "t1", "--yes")
    assert result.exit_code == 0, result.stdout
    start_call = [c for c in server.calls if c.path.endswith("/StartTask")]
    assert len(start_call) == 1
    assert start_call[0].request.id == "t1"
    assert start_call[0].request.expected_revision == "rev-9"

    # stop：显式 revision 时不得再发 GetTask（幂等/减少往返）
    server.respond_value(
        _p(server, "StopTask"),
        common_pb2.OperationResult(accepted=True, actual_state="STOPPED"),
    )
    calls_before = len(server.calls)
    result = cli_env.subprocess_invoke(
        "collector", "stop", "t1", "--expected-revision", "rev-10", "--yes"
    )
    assert result.exit_code == 0, result.stdout
    stop_call = [c for c in server.calls if c.path.endswith("/StopTask")]
    assert stop_call[-1].request.expected_revision == "rev-10"
    assert all(not c.path.endswith("/GetTask") for c in server.calls[calls_before:])


def test_start_requires_confirmation(cli_env: Any) -> None:
    """无 --yes 非 TTY：退出 2，且无任何 RPC。"""
    result = cli_env.subprocess_invoke("collector", "start", "t1")
    assert result.exit_code == 2, result.stdout
    assert not cli_env.server.calls


def test_revision_conflict_failed_precondition_exit1(cli_env: Any) -> None:
    """S03：乐观锁冲突 FAILED_PRECONDITION → 1，不自动重试。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "GetTask"),
        col.GetTaskResponse(task=col.Task(task_id="t1", revision="rev-x")),
    )
    server.fail(_p(server, "StartTask"), grpc.StatusCode.FAILED_PRECONDITION, "rev conflict")
    result = cli_env.subprocess_invoke("collector", "start", "t1", "--yes")
    assert result.exit_code == 1, result.stdout
    starts = [c for c in server.calls if c.path.endswith("/StartTask")]
    assert len(starts) == 1, "版本冲突不得自动重试"


def test_missing_revision_fails_closed_exit2(cli_env: Any) -> None:
    """S03：服务端未收敛（取不到 revision）必须 fail-closed，禁止无锁写入。"""
    server = cli_env.server
    server.respond_value(_p(server, "GetTask"), col.GetTaskResponse())
    result = cli_env.subprocess_invoke("collector", "stop", "t1", "--yes")
    assert result.exit_code == 2, result.stdout
    assert not [c for c in server.calls if c.path.endswith("/StopTask")]
