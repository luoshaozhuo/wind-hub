"""§14.4 表 14-2 崩溃恢复系统测试（UR-15，UC-08-S01）。

真实链路：gRPC 受理 → write-behind 真实落盘 WAL → 模拟进程崩溃（不做
优雅 I/O 收敛，直接释放持久化句柄）→ 以新 writer 重开同一 SQLite 文件
执行保守恢复裁决。

环境说明：崩溃在测试进程内模拟（OS 进程级 kill -9 + 独立进程重开的
变体在验收报告中记 NOT_RUN）；文件锁、WAL 重放与恢复裁决均为真实
SQLite 组件行为。恢复路径不创建任何协议会话、不重发旧写入。
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from commander.domain.command import CommandState
from commander.infrastructure.event_repository import EventRepository
from contracts import commander_pb2 as pb
from contracts import common_pb2

pytestmark = [
    pytest.mark.system,
    pytest.mark.reliability,
    pytest.mark.real_service,
]

# RFC5737 TEST-NET-1：SYN 无响应，命令在崩溃窗口内不可能到达终态。
_BLACKHOLE_HOST = "192.0.2.1"
_RECOVERED_STATES = {CommandState.UNKNOWN, CommandState.INTERRUPTED}


def _tz01_to_blackhole(service_dir: Path) -> None:
    devices = service_dir / "config" / "instances" / "devices.yaml"
    text = devices.read_text(encoding="utf-8")
    prefix, rest = text.split("  TZ01:", 1)
    block, suffix = rest.split("  TZ02:", 1)
    block = block.replace("host: 127.0.0.1", f"host: {_BLACKHOLE_HOST}", 1)
    devices.write_text(prefix + "  TZ01:" + block + "  TZ02:" + suffix, encoding="utf-8")


@pytest.mark.recovery
@pytest.mark.exception
@pytest.mark.parametrize("grpc_stack_prepared", [_tz01_to_blackhole], indirect=True)
async def test_crash_recovery(sys_pki, grpc_stack_prepared) -> None:
    """UC-08-S01：崩溃后只恢复已落盘事件并保守裁决，绝不重发旧写入。"""
    stack = grpc_stack_prepared
    _common, commander = stack.stubs(sys_pki, client="ops")
    devices = await commander.ListDevices(pb.ListDevicesRequest(), timeout=5)
    revision = next(d.revision for d in devices.devices if d.device_id == "TZ01")
    points = await commander.ListPoints(pb.ListPointsRequest(device_id="TZ01"), timeout=5)
    point = next(p for p in points.points if p.writable)
    submitted = await commander.SubmitCommand(
        pb.SubmitCommandRequest(
            device_id="TZ01",
            point_id=point.point_id,
            value=common_pb2.ScalarValue(sint64_value=1),
            expected_revision=revision,
        ),
        timeout=5,
    )
    command_id = submitted.command_id

    # 等待事件真实落盘（write-behind），但连接仍悬挂、命令无终态。
    service = stack.service
    writer = service._writer  # noqa: SLF001
    journal = service._journal  # noqa: SLF001
    deadline = asyncio.get_running_loop().time() + 5
    persisted = ()
    while asyncio.get_running_loop().time() < deadline:
        persisted = writer.submit(journal.get, command_id).result()
        if persisted is not None:
            break
        await asyncio.sleep(0.02)
    assert persisted is not None, "accepted event was not persisted before crash"
    assert persisted.state in {CommandState.ACCEPTED, CommandState.CONNECTING}

    # 模拟进程崩溃：不执行 executor.close 的 I/O 收敛，直接释放数据库句柄
    # （等价于进程死亡后 OS 回收 fd 并释放 flock）。
    writer.submit(journal.close).result()

    # 新进程视角：新单 writer 线程重开同一真实数据库。
    second_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sys-recover")
    try:
        reopened = second_writer.submit(lambda: EventRepository(stack.db_path)).result()
        try:
            recovered = second_writer.submit(reopened.recover).result()
            assert command_id in set(recovered)
            record = second_writer.submit(reopened.get, command_id).result()
            assert record is not None
            assert record.state in _RECOVERED_STATES
            # 保守裁决绝不等于成功；I/O 未安全结束时不得声称 settled 语义。
            assert record.state is not CommandState.SUCCEEDED
            # 恢复幂等：第二次恢复不产生新裁决（不重放、不重复写事件）。
            assert second_writer.submit(reopened.recover).result() == ()
        finally:
            second_writer.submit(reopened.close).result()
    finally:
        second_writer.shutdown(wait=True)
