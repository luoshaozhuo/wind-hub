"""跨重启 request_id 幂等与聚合回查（语义⑧）——真实 gRPC/mTLS + 真实 SQLite。

在测试进程内按生产组合根重建 CommanderService 模拟独立进程崩溃重启：
先停止 gRPC 入口，再以“进程被 kill”的方式释放全部句柄——取消审计任务、
排空持久化 worker、关闭 SQLite 连接（flock 随 fd 关闭真实释放），随后
build_commander 同一 yaml 重开并监听新安全端口。OS 进程级 kill/重启变体
在验收报告记 NOT_RUN；本测试覆盖的句柄释放、flock 重开、WAL 重放与
启动恢复均为真实行为。

不使用优雅停机的原因：harness 的 NATS 指向不可达地址，nats-py 会在
connect() 内长重试，优雅 stop 在有未投递审计事件时按生产语义置 FAILED
并故意保留文件锁；进程被 kill 时 OS 直接回收 fd/flock，本助手忠实复刻
这一释放过程，而不是修改生产停机策略。

验证：
1. 重启前已终态的请求，重启后按原 request_id 聚合回查得到同一
   command_id，终态投影不丢失；
2. 同 caller + 同 request_id + 同指纹重放仍返回原命令（持久层去重，
   非内存缓存），Modbus 从站写入计数不增；
3. 同 request_id 异指纹在重启后仍被 ALREADY_EXISTS 拒绝；
4. 新生命周期受理计数从 0 开始，重启不重发任何旧命令。
"""

from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4

import grpc
import pytest

from commander.application.service_lifecycle import CommanderService
from commander.assembly import build_commander
from commander.infrastructure.adapters.inbound.grpc.server import register_services
from commander.infrastructure.adapters.inbound.grpc.snapshots import ConfigSnapshots
from contracts import commander_pb2 as pb
from contracts import common_pb2
from core.application.protocol_registry import ProtocolRegistry
from core.infrastructure.protocol.modbus import ModbusDriver
from tests.commander.grpc_harness import GrpcStack, build_pki, build_stack
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.process import free_port

pytestmark = [pytest.mark.system, pytest.mark.reliability, pytest.mark.real_service]


async def _revision(commander) -> str:
    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    return str(next(device.revision for device in devices.devices if device.device_id == "WT01"))


def _request(revision: str, request_id: str, *, value: float) -> pb.SubmitCommandRequest:
    return pb.SubmitCommandRequest(
        device_id="WT01",
        point_id="setpoint_power",
        value=common_pb2.ScalarValue(double_value=value),
        expected_revision=revision,
        request_id=request_id,
    )


async def _simulate_process_kill(server: grpc.aio.Server, service: CommanderService) -> None:
    """模拟 OS 回收进程：入口停收、后台任务取消、SQLite 句柄与文件锁真实释放。"""
    await server.stop(grace=5)
    watchdog = service._watchdog
    if watchdog is not None:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watchdog
    idle_tasks = tuple(service.executor._idle_tasks.values())
    for task in idle_tasks:
        task.cancel()
    for task in idle_tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    audit = service._audit
    if audit is not None and audit._task is not None:
        audit._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(audit._task), timeout=3.0)
    persistence = service._persistence
    if persistence is not None:
        # 先排空 write-behind 事件，保证 WAL 中的命令历史与崩溃前裁决一致。
        with contextlib.suppress(Exception):
            await persistence.stop(timeout=5.0)
    loop = asyncio.get_running_loop()
    journal = service._journal
    writer = service._writer
    assert journal is not None and writer is not None
    await loop.run_in_executor(writer, journal.close)
    await asyncio.to_thread(writer.shutdown, wait=True)
    if service._buffer is not None:
        service._buffer.close()


async def _restart_server(
    stack: GrpcStack, pki
) -> tuple[GrpcStack, grpc.aio.Server, CommanderService]:
    """同一 yaml 重建组合根并监听新安全端口；返回可复用 stubs 的新栈。"""
    registry = ProtocolRegistry()
    registry.register("modbus", ModbusDriver)
    service2 = build_commander(service_config=stack.yaml_path, protocol_registry=registry)
    await service2.start()
    snapshots2 = ConfigSnapshots(service2, stack.yaml_path)
    server2 = grpc.aio.server(maximum_concurrent_rpcs=100)
    register_services(server2, service2, stack.permissions, snapshots2)
    credentials = grpc.ssl_server_credentials(
        [(pki.key("server"), pki.cert("server"))],
        root_certificates=pki.ca_cert,
        require_client_auth=True,
    )
    port = free_port()
    assert server2.add_secure_port(f"127.0.0.1:{port}", credentials)
    await server2.start()
    restarted = GrpcStack(
        service=service2,
        snapshots=snapshots2,
        target=f"127.0.0.1:{port}",
        yaml_path=stack.yaml_path,
        root=stack.root,
        db_path=stack.db_path,
        permissions=stack.permissions,
        stream=stack.stream,
        subject=stack.subject,
    )
    return restarted, server2, service2


async def test_request_id_survives_restart_with_durable_dedup(
    tmp_path, modbus_server: ModbusMockServer
) -> None:
    pki = build_pki(tmp_path)
    stack, server, service = await build_stack(
        tmp_path,
        pki,
        modbus_port=modbus_server.port,
        label="src-restart",
        stream_prefix="CMD_SRC_RESTART",
    )
    crashed = False
    try:
        _common, commander = stack.stubs(pki, client="ops")
        revision = await _revision(commander)
        request_id = f"req-restart-{uuid4().hex}"
        submitted = await commander.SubmitCommand(
            _request(revision, request_id, value=42.5), timeout=5
        )
        original_command_id = submitted.command_id
        # 等真实 Modbus 写入与终态收敛。
        deadline_view = None
        for _ in range(200):
            view = await commander.GetRequestResult(
                pb.GetRequestResultRequest(request_id=request_id), timeout=5
            )
            if view.state in {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"}:
                deadline_view = view
                break
            await asyncio.sleep(0.05)
        assert deadline_view is not None and deadline_view.state == "SUCCEEDED"
        writes_before_restart = modbus_server.write_count

        # “进程崩溃”：释放全部句柄与 flock，数据库文件保持崩溃前的真实状态。
        await _simulate_process_kill(server, service)
        crashed = True
        restarted, server2, service2 = await _restart_server(stack, pki)
        try:
            _common2, commander2 = restarted.stubs(pki, client="ops")
            # health.control_accepted_total 是启动时刻的 journal 快照：
            # 恰为本测试崩溃前受理的 1 个请求，证明恢复未注入新 ACCEPTED 行；
            # 不重发的直接证据是 Modbus 从站写入计数保持不变。
            assert service2.health().control_accepted_total == 1
            assert modbus_server.write_count == writes_before_restart

            # 持久聚合回查：同一 request_id、同一 command_id、终态不丢失。
            view = await commander2.GetRequestResult(
                pb.GetRequestResultRequest(request_id=request_id), timeout=5
            )
            assert [item.command_id for item in view.commands] == [original_command_id]
            assert view.commands[0].state == "SUCCEEDED"
            assert view.io_settled is True

            # 持久层去重（非内存缓存）：同指纹重放返回原命令，零新增现场写。
            replay = await commander2.SubmitCommand(
                _request(revision, request_id, value=42.5), timeout=5
            )
            assert replay.command_id == original_command_id
            assert modbus_server.write_count == writes_before_restart

            # 异指纹冲突在重启后仍然成立。
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await commander2.SubmitCommand(
                    _request(revision, request_id, value=99.9), timeout=5
                )
            assert exc_info.value.code() is grpc.StatusCode.ALREADY_EXISTS
            assert modbus_server.write_count == writes_before_restart
        finally:
            await server2.stop(grace=5)
            with contextlib.suppress(Exception):
                await service2.stop()
            audit2 = service2._audit
            if audit2 is not None and audit2._task is not None:
                audit2._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await audit2._task
    finally:
        # 崩溃路径已显式释放全部句柄；仅在断言提前失败时做兜底清理。
        if not crashed:
            with contextlib.suppress(Exception):
                await server.stop(grace=1)
            with contextlib.suppress(Exception):
                await service.stop()
