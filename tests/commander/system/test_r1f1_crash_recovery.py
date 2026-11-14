"""R1-F1 M-P1-03：独立进程崩溃后的恢复与设备隔离（真实 SQLite + 真实服务）。

子进程写入真实 SQLite T1/T2 后 os._exit() 模拟崩溃（不关闭连接、不走
任何清理路径），父进程以相同配置重启 CommanderService：
  - ACCEPTED 后崩溃、EXECUTING 后崩溃的命令均恢复为 UNKNOWN；
  - 受影响设备在可信隔离证据前保持 BLOCKED，服务 DEGRADED 而非 READY；
  - 不自动重发旧写入，不受理新的冲突控制命令；
  - Watchdog 不得在无证据时清除阻断；停机如实报告残留设备。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from commander.application.service_lifecycle import ServiceState
from commander.domain.command import CommandSpec

from .test_service_e2e_real_stack import _fingerprint, _start_service

pytestmark = [
    pytest.mark.system,
    pytest.mark.modbus,
    pytest.mark.real_service,
    pytest.mark.docker,
]

# 子进程：真实 SQLite 留下一条 EXECUTING（T2 后崩溃）与一条 ACCEPTED
# （T1 后崩溃）命令，随后 os._exit 模拟进程崩溃，绝不走正常关闭。
_CHILD = r"""
import json, os, sys
from commander.application.persistence import BufferedJournal
from commander.domain.command import CommandSpec, CommandState
from commander.infrastructure.event_repository import EventRepository

db_path, fingerprint = sys.argv[1], sys.argv[2]
journal = EventRepository(db_path)
buffer = BufferedJournal()
executing = buffer.accept(
    [CommandSpec("WT01", "setpoint_power", 10.0, fingerprint)],
    request_id="req-crash-executing", caller_id="ops-console",
)
accepted_only = buffer.accept(
    [CommandSpec("WT01", "setpoint_power", 20.0, fingerprint)],
    request_id="req-crash-accepted", caller_id="ops-console",
)
buffer.transition(executing[0].command_id, CommandState.CONNECTING)
buffer.transition(executing[0].command_id, CommandState.EXECUTING)
journal.append_events(buffer.snapshot(100))
sys.stdout.write(json.dumps({
    "executing": executing[0].command_id,
    "accepted": accepted_only[0].command_id,
}))
sys.stdout.flush()
os._exit(0)  # 模拟崩溃：不关闭 SQLite 连接、不执行任何清理。
"""


async def test_independent_process_crash_recovers_unknown_and_blocks_device(
    config_tree, modbus_server, jetstream_url
) -> None:
    root, db_path = config_tree
    service, _stream, _subject = await _start_service(
        root, db_path, jetstream_url, modbus_server.port
    )
    fingerprint = _fingerprint(service)
    assert await service.stop() == frozenset()

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
    crashed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _CHILD, str(db_path), fingerprint],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    command_ids = json.loads(crashed.stdout)

    service2, _s2, _j2 = await _start_service(root, db_path, jetstream_url, modbus_server.port)
    try:
        health = service2.health()
        # ACCEPTED 后崩溃 + EXECUTING 后崩溃：两条都保守恢复为 UNKNOWN。
        assert health.recovered_commands == 2
        assert health.state is ServiceState.DEGRADED
        assert health.ready is False
        assert service2.executor.blocked_devices() == frozenset({"WT01"})
        for key in ("executing", "accepted"):
            recovered = await service2.control.get_command(command_ids[key])
            assert recovered is not None and recovered.outcome is not None
            assert recovered.state.value == "UNKNOWN", key
            assert recovered.outcome.value == "UNKNOWN", key
        # 未确认隔离的设备不受理新冲突命令；不自动重发旧写入。
        fingerprint2 = _fingerprint(service2)
        with pytest.raises(RuntimeError, match="device unavailable"):
            await service2.executor.accept(
                [CommandSpec("WT01", "setpoint_power", 99.0, fingerprint2)],
                request_id=f"req-e2e-{uuid4().hex}",
                caller_id="ops-console",
            )
        assert modbus_server.write_count == 0
        # Watchdog 运行期间不得在无物理证据时清除 BLOCKED / 提前 READY。
        await asyncio.sleep(1.5)
        assert service2.health().state is ServiceState.DEGRADED
        assert service2.executor.blocked_devices() == frozenset({"WT01"})
        assert modbus_server.write_count == 0
        # F1-GAP-01：无本地 Session=无可信证据，显式解除必须失败关闭；
        # 解除失败后 Watchdog 仍不得把服务错误恢复为 READY。
        assert await service2.executor.isolate_device("WT01") is False
        await asyncio.sleep(1.0)
        assert service2.health().state is ServiceState.DEGRADED
        assert service2.executor.blocked_devices() == frozenset({"WT01"})
        assert modbus_server.write_count == 0
    finally:
        # 停机必须显式报告残留阻断设备，不得伪装干净 STOPPED。
        assert await service2.stop() == frozenset({"WT01"})
