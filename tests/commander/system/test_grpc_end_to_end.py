"""§14.4 表 14-2 系统级 gRPC 端到端（UR-07/08/09/11/12 主路径与异常）。

真实 mTLS（TCP 回环 TLS 1.3 握手）、build_commander 组合根、真实共享配置
树、真实磁盘 SQLite；不使用任何 gRPC/配置替身。环境说明：组合根在测试
进程内重建（OS 进程级隔离的独立进程变体见验收报告 NOT_RUN 项），但网络
边界、TLS、文件系统与持久化均为真实组件。
"""

from __future__ import annotations

import grpc
import pytest

from contracts import common_pb2

pytestmark = [pytest.mark.system, pytest.mark.reliability, pytest.mark.real_service]

DISK = common_pb2.SNAPSHOT_SCOPE_DISK
RUNTIME = common_pb2.SNAPSHOT_SCOPE_RUNTIME
CANDIDATE = common_pb2.SNAPSHOT_SCOPE_CANDIDATE


async def _aborts(call, code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await call
    assert exc_info.value.code() is code, exc_info.value
    return exc_info.value


async def _disk(common) -> common_pb2.GetConfigSnapshotResponse:
    return await common.GetConfigSnapshot(
        common_pb2.GetConfigSnapshotRequest(scope=DISK), timeout=10
    )


@pytest.mark.normal
@pytest.mark.exception
async def test_service_info_status(sys_pki, grpc_stack) -> None:
    """UR-07/UR-08 UC-04-S01：服务身份/依赖健康真实返回；存活不等于就绪。"""
    common_viewer, _commander = grpc_stack.stubs(sys_pki, client="viewer")
    info = await common_viewer.GetServiceInfo(common_pb2.Empty(), timeout=5)
    assert info.service_id == "commander"
    assert info.process_role == "control"
    assert info.contract_version == "v1"
    status = await common_viewer.GetServiceStatus(common_pb2.Empty(), timeout=5)
    deps = {dep.name: dep for dep in status.dependencies}
    assert set(deps) == {"persistence", "journal", "audit", "dispatcher"}
    # 依赖健康以封闭枚举真实报告；本环境无待发审计事件时 NATS 尚未被触达，
    # 不预设 audit 必为降级——其状态在产生发箱流量后才会反映不可达。
    assert deps["persistence"].health == "HEALTHY"
    assert deps["journal"].health == "HEALTHY"
    assert deps["audit"].health in {"HEALTHY", "DEGRADED"}
    assert status.health in {"HEALTHY", "READY", "DEGRADED"}
    # service_ready 指标表达业务就绪，而不是"进程还活着"。
    metrics = await common_viewer.GetMetrics(
        common_pb2.GetMetricsRequest(scope="commander", window_ms=0), timeout=5
    )
    ready = {m.name: m for m in metrics.metrics}["service_ready"]
    assert ready.value in (0.0, 1.0)
    # 无权限身份在系统边界被拒（异常侧）。
    common_stranger, _ = grpc_stack.stubs(sys_pki, client="limited")
    await _aborts(
        common_stranger.GetServiceStatus(common_pb2.Empty(), timeout=5),
        grpc.StatusCode.PERMISSION_DENIED,
    )


@pytest.mark.normal
@pytest.mark.concurrency
async def test_config_snapshot_apply(sys_pki, grpc_stack) -> None:
    """The Repository is the only publisher; legacy writes remain fail-closed."""
    common, _ = grpc_stack.stubs(sys_pki, client="deployer")
    disk = await _disk(common)
    await _aborts(
        common.PushConfigSnapshot(
            common_pb2.PushConfigSnapshotRequest(files=disk.files, expected_revision=disk.revision),
            timeout=10,
        ),
        grpc.StatusCode.FAILED_PRECONDITION,
    )
    await _aborts(
        common.ApplyConfigSnapshot(
            common_pb2.ApplyConfigSnapshotRequest(
                candidate_id="unpublished", expected_revision=disk.revision
            ),
            timeout=10,
        ),
        grpc.StatusCode.FAILED_PRECONDITION,
    )
    assert (await _disk(common)).revision == disk.revision


@pytest.mark.exception
async def test_config_revision_conflict(sys_pki, grpc_stack) -> None:
    """Even an invented revision cannot bypass sole Repository publication."""
    common, _ = grpc_stack.stubs(sys_pki, client="deployer")
    disk = await _disk(common)
    await _aborts(
        common.PushConfigSnapshot(
            common_pb2.PushConfigSnapshotRequest(files=disk.files, expected_revision="0" * 64),
            timeout=10,
        ),
        grpc.StatusCode.FAILED_PRECONDITION,
    )
    assert (await _disk(common)).revision == disk.revision


@pytest.mark.normal
@pytest.mark.exception
async def test_config_snapshot_scopes(sys_pki, grpc_stack) -> None:
    """DISK/RUNTIME are readable; a candidate cannot be created locally."""
    common, _ = grpc_stack.stubs(sys_pki, client="deployer")
    disk = await _disk(common)
    runtime = await common.GetConfigSnapshot(
        common_pb2.GetConfigSnapshotRequest(scope=RUNTIME), timeout=10
    )
    assert disk.files and runtime.files
    assert disk.revision != runtime.revision
    await _aborts(
        common.GetConfigSnapshot(
            common_pb2.GetConfigSnapshotRequest(scope=CANDIDATE, candidate_id="unpublished"),
            timeout=5,
        ),
        grpc.StatusCode.NOT_FOUND,
    )
    assert (await _disk(common)).revision == disk.revision


@pytest.mark.normal
@pytest.mark.exception
async def test_metrics_events_diagnose(sys_pki, grpc_stack) -> None:
    """UR-07/UR-08 UC-04-S03：三个只读通道真实可用，且诊断绝不触发控制写入。"""
    common, commander = grpc_stack.stubs(sys_pki, client="ops")
    metrics = await common.GetMetrics(
        common_pb2.GetMetricsRequest(scope="commander", window_ms=1000), timeout=5
    )
    names = {m.name for m in metrics.metrics}
    # §13.2 封闭指标的最小必选子集真实存在。
    # device_queue_depth 仅在 device:<id> 作用域返回（§13.3），不属于服务级。
    assert {
        "control_accepted_total",
        "control_completed_total",
        "pending_global",
        "audit_pending_count",
        "service_ready",
    } <= names
    events_before = await common.GetEvents(common_pb2.GetEventsRequest(), timeout=5)
    diagnose = await common.Diagnose(
        common_pb2.DiagnoseRequest(
            stages=["service", "configuration", "tcp"],
            target_id="TZ01",
            timeout_ms=2000,
        ),
        timeout=8,
    )
    assert {s.stage for s in diagnose.stages} == {"service", "configuration", "tcp"}
    # 诊断后无新增控制事件、队列深度为 0：只读通道没有隐式写 PLC。
    events_after = await common.GetEvents(common_pb2.GetEventsRequest(), timeout=5)
    assert len(events_after.events) == len(events_before.events)
    device_metrics = await common.GetMetrics(
        common_pb2.GetMetricsRequest(scope="device:TZ01", window_ms=0), timeout=5
    )
    depth = next(m for m in device_metrics.metrics if m.name == "device_queue_depth")
    assert depth.value == 0.0
