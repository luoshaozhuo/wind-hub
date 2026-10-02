"""OperationManager 单元测试。

验证阶段：unit。仅验证内存状态机，不启动后台任务、FastAPI 或 Runtime；
不能证明跨进程持久化，因为 Phase 1 明确采用进程内实现。
"""

import pytest

from wind_hub_server.application.operation import OperationManager, OperationState


def test_operation_lifecycle_and_progress() -> None:
    """Operation 应稳定经历 pending → running → success。"""
    manager = OperationManager()
    created = manager.create("devices.verify_all", total=4)

    running = manager.mark_running(created.operation_id)
    progressed = manager.update_progress(created.operation_id, completed=2)
    finished = manager.succeed(created.operation_id, {"failed": 0})

    assert running.state is OperationState.RUNNING
    assert progressed.progress == 0.5
    assert finished.state is OperationState.SUCCESS
    assert finished.completed == 4
    assert finished.progress == 1.0
    assert finished.result == {"failed": 0}


def test_operation_failure_uses_stable_error() -> None:
    """失败 Operation 应保留稳定错误码与 details。"""
    manager = OperationManager()
    created = manager.create("config.apply")

    failed = manager.fail(
        created.operation_id, code="CONFIG_APPLY_FAILED", message="apply failed",
        details={"file": "devices.yaml"},
    )

    assert failed.state is OperationState.FAILED
    assert failed.error is not None
    assert failed.error.code == "CONFIG_APPLY_FAILED"


def test_unknown_operation_raises_key_error() -> None:
    """未知 ID 不得伪造默认 Operation。"""
    manager = OperationManager()
    with pytest.raises(KeyError):
        manager.get("missing")

def test_operation_partial_and_cancel_terminal_states() -> None:
    """批量部分成功与取消必须有公开状态推进接口。"""
    manager = OperationManager()
    partial_op = manager.create("devices.verify_all", total=2)
    cancelled_op = manager.create("diagnostics.scan")

    partial = manager.complete_partial(partial_op.operation_id, {"failed": 1})
    cancelled = manager.cancel(cancelled_op.operation_id)

    assert partial.state is OperationState.PARTIAL
    assert partial.result == {"failed": 1}
    assert cancelled.state is OperationState.CANCELLED

def test_terminal_operation_cannot_be_overwritten() -> None:
    """终态不可被迟到 worker 二次覆盖。"""
    manager = OperationManager()
    created = manager.create("diagnostics.scan", total=1)
    manager.mark_running(created.operation_id)
    manager.succeed(created.operation_id, {"ok": True})

    with pytest.raises(ValueError, match="terminal"):
        manager.fail(
            created.operation_id,
            code="LATE_ERROR",
            message="late worker result",
        )


def test_progress_requires_running_state() -> None:
    """pending Operation 不允许直接推进 progress。"""
    manager = OperationManager()
    created = manager.create("diagnostics.scan", total=2)

    with pytest.raises(ValueError, match="running"):
        manager.update_progress(created.operation_id, completed=1)
