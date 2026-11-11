"""§14.4 UC-01/08/09/10 生命周期系统测试（真实三进程，无 Mock）。

覆盖 v1_21 表 14-2 中与进程生命周期相关的规定场景，以及 Round-3 验收
S3 优先级场景中的故障注入项：

- UC-01-S01：单容器三进程（Main + realtime/reliable 两个 spawn worker）
  启动就绪，进程/业务就绪分离；
- S3-05：单个 worker 被 SIGKILL 后 Main 只补位丢失角色，gRPC 控制面
  全程可达，存活 worker 不受影响；
- S3-04：worker 被 SIGSTOP（IPC 停滞）时报告 UNKNOWN 而非假 RUNNING，
  停滞期间绝不产生重复角色进程（无双所有权），恢复后重新收敛；
- S3-06：Main 整体重启后按持久化意图自动恢复 START 的 Delivery，重启
  过程不调用任何 StartDelivery 控制 RPC；
- S3-07：手动 STOP 是持久意图，跨看门狗周期与 Main 重启都不得复活。

所有断言位于进程边界：mTLS gRPC 正式 Stub、``/proc`` PID 采样、真实
NATS/JetStream、Redis、Kafka（R5 一次性 Docker 栈）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Awaitable, Callable

import pytest

from contracts import common_pb2, common_pb2_grpc, sink_service_pb2, sink_service_pb2_grpc
from tests.sink_service.system.conftest import (
    REALTIME_DELIVERY,
    RELIABLE_DELIVERY,
    SinkSystemHarness,
    role_pids,
    worker_pid_set,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.reliability,
    pytest.mark.docker,
    pytest.mark.real_service,
    pytest.mark.slow,
    pytest.mark.nats,
    pytest.mark.normal,
    pytest.mark.recovery,
]


async def _delivery_states(harness: SinkSystemHarness) -> dict[str, str]:
    async with harness.channel() as channel:
        response = await sink_service_pb2_grpc.SinkServiceStub(channel).ListDeliveries(
            sink_service_pb2.ListDeliveriesRequest()
        )
    return {item.delivery_id: item.actual_state for item in response.deliveries}


async def _worker_health(harness: SinkSystemHarness) -> dict[str, str]:
    async with harness.channel() as channel:
        status = await common_pb2_grpc.CommonServiceStub(channel).GetServiceStatus(
            common_pb2.Empty()
        )
    return {
        dep.name.removeprefix("worker."): dep.health
        for dep in status.dependencies
        if dep.name.startswith("worker.")
    }


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]], timeout: float, description: str
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    last: object = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await predicate():
                return
        except Exception as exc:  # noqa: BLE001 - polling loop records the last failure
            last = exc
        await asyncio.sleep(0.5)
    raise AssertionError(f"timed out waiting for {description}; last={last!r}")


async def _start_delivery(harness: SinkSystemHarness, delivery_id: str) -> str:
    async with harness.channel() as channel:
        detail = await sink_service_pb2_grpc.SinkServiceStub(channel).GetDelivery(
            common_pb2.ObjectId(id=delivery_id)
        )
        result = await sink_service_pb2_grpc.SinkServiceStub(channel).StartDelivery(
            common_pb2.OperationRequest(id=delivery_id, expected_revision=detail.delivery.revision)
        )
    assert result.accepted, result.message
    return result.revision


async def _stop_delivery(harness: SinkSystemHarness, delivery_id: str) -> str:
    async with harness.channel() as channel:
        detail = await sink_service_pb2_grpc.SinkServiceStub(channel).GetDelivery(
            common_pb2.ObjectId(id=delivery_id)
        )
        result = await sink_service_pb2_grpc.SinkServiceStub(channel).StopDelivery(
            common_pb2.OperationRequest(id=delivery_id, expected_revision=detail.delivery.revision)
        )
    assert result.accepted, result.message
    return result.revision


@pytest.mark.normal
class TestThreeProcessLifecycle:
    async def test_uc01_s01_three_process_topology_and_readiness(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """UC-01-S01：Main 就绪时两个 worker 角色齐备且业务状态 RUNNING。"""
        sink_harness.write_dual_channel_root()
        await sink_harness.provision_streams()
        proc = await sink_harness.start()
        try:
            ready = await sink_harness.wait_grpc_ready()
            assert ready["service_id"] == "sink-west"

            children = await _wait_two_workers(proc.proc.pid)
            assert set(children) == {"realtime", "reliable"}

            await _wait_until(
                lambda: _both_running(sink_harness),
                timeout=30,
                description="both workers RUNNING",
            )
            states = await _delivery_states(sink_harness)
            assert states == {
                REALTIME_DELIVERY: "RUNNING",
                RELIABLE_DELIVERY: "RUNNING",
            }
        finally:
            await sink_harness.stop()


async def _both_running(harness: SinkSystemHarness) -> bool:
    return await _worker_health(harness) == {"realtime": "RUNNING", "reliable": "RUNNING"}


async def _wait_two_workers(main_pid: int, timeout: float = 30.0) -> dict[str, int]:
    """轮询直到 Main 下出现两个角色 worker（spawn/重建收敛可能晚于 gRPC 就绪）。"""

    async def _two_workers() -> bool:
        return await asyncio.to_thread(lambda: len(worker_pid_set(main_pid)) == 2)

    await _wait_until(_two_workers, timeout, "two spawn workers present")
    return role_pids(main_pid)


@pytest.mark.failure
@pytest.mark.recovery
class TestWorkerFaultInjection:
    async def test_s3_05_sigkill_respawns_only_lost_role(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """S3-05：SIGKILL 一个 worker，仅该角色补位，控制面零拒绝。"""
        sink_harness.write_dual_channel_root()
        await sink_harness.provision_streams()
        proc = await sink_harness.start()
        try:
            await _wait_until(lambda: _both_running(sink_harness), 30, "initial RUNNING")
            before = role_pids(proc.proc.pid)
            victim_role = "realtime"
            victim = before[victim_role]
            survivor = before["reliable"]

            os.kill(victim, signal.SIGKILL)
            respawned: int | None = None
            refused = 0
            max_children = 2
            deadline = asyncio.get_running_loop().time() + 40
            while asyncio.get_running_loop().time() < deadline:
                try:
                    await _worker_health(sink_harness)
                except Exception:  # noqa: BLE001 - count control-plane refusals
                    refused += 1
                # 重建后启动顺序映射会互换，只能用 PID 集合差异识别补位进程。
                pids = worker_pid_set(proc.proc.pid)
                max_children = max(max_children, len(pids))
                if victim not in pids and survivor in pids and len(pids) == 2:
                    respawned = (pids - {survivor}).pop()
                    break
                await asyncio.sleep(0.5)

            assert respawned is not None, "lost role was not respawned"
            assert respawned != victim
            assert survivor in worker_pid_set(proc.proc.pid), "surviving role was disturbed"
            # 任何采样点都不允许出现第三个角色 worker（禁止并行同角色消费者）。
            assert max_children == 2, f"dual ownership detected, peak children={max_children}"
            assert refused == 0, f"Main gRPC had {refused} unavailable ticks during respawn"
            await _wait_until(lambda: _both_running(sink_harness), 30, "reconverged RUNNING")
        finally:
            await sink_harness.stop()

    async def test_s3_04_sigstop_reports_unknown_without_dual_ownership(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """S3-04：IPC 停滞报 UNKNOWN；失活 worker 先隔离终止再重建，绝不并行双开。

        v1_21 第 6 章：旧子进程还活着但失去 IPC 时，Main 不得并行启动第二
        个同角色消费者，必须先确认旧 PID 终止。因此 SIGSTOP 后允许 Supervisor
        杀死并重建停滞 PID（SIGKILL 对停止态进程仍生效），但任何采样点角色
        worker 总数都不得超过 2，且另一角色 PID 全程不变。
        """
        sink_harness.write_dual_channel_root()
        await sink_harness.provision_streams()
        proc = await sink_harness.start()
        try:
            await _wait_until(lambda: _both_running(sink_harness), 30, "initial RUNNING")
            before = role_pids(proc.proc.pid)
            stalled_role = "reliable"
            stalled_pid = before[stalled_role]
            healthy_pid = before["realtime"]

            os.kill(stalled_pid, signal.SIGSTOP)
            try:
                await _wait_until(
                    lambda: _role_unknown(sink_harness, stalled_role),
                    timeout=25,
                    description=f"{stalled_role} UNKNOWN while stopped",
                )
                # 跨越 IPC 超时（5s）+ 监督周期（1s）+ 隔离 join（3s）+
                # 重建就绪（≤10s）整个窗口：逐秒采样进程边界。
                max_children = 0
                isolated = False
                for _ in range(12):
                    pids = worker_pid_set(proc.proc.pid)
                    max_children = max(max_children, len(pids))
                    # 另一角色全程不受影响、不被重建。
                    assert healthy_pid in pids, "healthy role PID changed during isolation"
                    # 设计红线：任何时刻都不允许两个同角色 worker 并存。
                    assert len(pids) <= 2, f"dual ownership detected: {pids}"
                    health = await _worker_health(sink_harness)
                    assert health["realtime"] == "RUNNING"
                    # 停滞角色隔离前必须诚实报 UNKNOWN；重建完成后允许恢复
                    # RUNNING（新 PID 的真实证据），但停滞期间绝不假报 RUNNING。
                    assert health[stalled_role] in {"UNKNOWN", "RUNNING"}
                    if stalled_pid not in pids:
                        isolated = True
                    await asyncio.sleep(1.0)
                assert max_children <= 2
                # 12s 窗口足以覆盖隔离+重建；旧 PID 应已被确认终止。
                assert isolated, "supervisor never isolated the stalled worker"
            finally:
                # 停滞 PID 可能已被 Supervisor 按设计 SIGKILL 隔离。
                with contextlib.suppress(ProcessLookupError):
                    os.kill(stalled_pid, signal.SIGCONT)

            await _wait_until(lambda: _both_running(sink_harness), 40, "post-resume RUNNING")
            after = role_pids(proc.proc.pid)
            assert set(after) == {"realtime", "reliable"}
            assert after["realtime"] == healthy_pid
        finally:
            await sink_harness.stop()


async def _role_unknown(harness: SinkSystemHarness, role: str) -> bool:
    health = await _worker_health(harness)
    return health.get(role) == "UNKNOWN"


@pytest.mark.recovery
class TestMainRestartAndManualStop:
    async def test_s3_06_main_restart_recovers_started_deliveries_without_rpc(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """S3-06：同配置根冷启动，零 StartDelivery 即恢复 RUNNING。"""
        sink_harness.write_dual_channel_root()
        await sink_harness.provision_streams()
        proc_a = await sink_harness.start()
        try:
            await _wait_until(
                lambda: _states_equal(
                    sink_harness,
                    {REALTIME_DELIVERY: "RUNNING", RELIABLE_DELIVERY: "RUNNING"},
                ),
                30,
                "initial deliveries RUNNING",
            )
            assert await sink_harness.stop() == 0
        finally:
            if sink_harness.process is not None:
                await sink_harness.stop()

        # 同配置根重新启动：不发任何 StartDelivery。
        proc_b = await sink_harness.start()
        try:
            assert proc_b.proc.pid != proc_a.proc.pid
            await _wait_until(
                lambda: _states_equal(
                    sink_harness,
                    {REALTIME_DELIVERY: "RUNNING", RELIABLE_DELIVERY: "RUNNING"},
                ),
                40,
                "deliveries auto-recovered after Main restart",
            )
        finally:
            await sink_harness.stop()

    async def test_s3_07_manual_stop_survives_watchdog_and_main_restart(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """S3-07：STOP 跨两个以上看门狗周期（5s/次）与 Main 重启保持。"""
        sink_harness.write_dual_channel_root()
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            await _wait_until(lambda: _both_running(sink_harness), 30, "initial RUNNING")
            await _stop_delivery(sink_harness, REALTIME_DELIVERY)
            await _wait_until(
                lambda: _delivery_is(sink_harness, REALTIME_DELIVERY, "STOPPED"),
                20,
                "manual STOP observed",
            )
            # 两个半看门狗周期，确认无复活。
            await asyncio.sleep(12.5)
            assert await _delivery_is(sink_harness, REALTIME_DELIVERY, "STOPPED")

            await sink_harness.stop()
            await sink_harness.start()
            try:
                await _wait_until(lambda: _both_running(sink_harness), 30, "workers back")
                # 持久 STOP 意图在冷启动后仍然成立。
                assert await _delivery_is(sink_harness, REALTIME_DELIVERY, "STOPPED")
                # 可靠通道未受影响。
                assert await _delivery_is(sink_harness, RELIABLE_DELIVERY, "RUNNING")
                # 显式 START 仍可恢复控制。
                await _start_delivery(sink_harness, REALTIME_DELIVERY)
                await _wait_until(
                    lambda: _delivery_is(sink_harness, REALTIME_DELIVERY, "RUNNING"),
                    20,
                    "manual START restores delivery",
                )
            finally:
                await sink_harness.stop()
        finally:
            if sink_harness.process is not None:
                await sink_harness.stop()


async def _states_equal(harness: SinkSystemHarness, expected: dict[str, str]) -> bool:
    return await _delivery_states(harness) == expected


async def _delivery_is(harness: SinkSystemHarness, delivery_id: str, state: str) -> bool:
    return (await _delivery_states(harness)).get(delivery_id) == state
