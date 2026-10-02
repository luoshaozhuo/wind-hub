"""Recovery：Modbus 协议断连全周期——正常 → 断连 → 检测 → 进程存活 → 恢复 → 数据恢复。

故障注入手段是 fixture server 的真实起停（同一端口），Collector 为独立
subprocess；检测与恢复信号全部来自系统边界：ctl status 的
``devices_connected``、输出文件行数增减、ctl read 的 RPC 错误。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.recovery.helpers import ctl_status, wait_status, write_modbus_file_config
from tests.system.process import CollectorProcess, run_ctl_async
from tests.system.wait import read_jsonl, wait_file_rows

pytestmark = pytest.mark.modbus

INSTANCE_ID = "modbus-telemetry:modbus-1"


class TestProtocolDisconnectRecovery:
    async def test_full_disconnect_recovery_cycle(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir)

        # ---- 正常：采集数据到达 sink ----
        assert (
            await run_ctl_async("start-instance", INSTANCE_ID, target=proc.grpc_target)
        ).ok
        await wait_file_rows(sink_path, min_rows=4)

        # ---- 故障：从站停止（同一端口随后恢复） ----
        await modbus_server.stop()

        # ---- 检测：设备健康在边界可见地翻转 ----
        await wait_status(
            proc,
            lambda p: p["devices_connected"] == 0,
            description="device detected disconnected",
        )
        # 数据面静默：先等残余缓冲落盘，再确认行数在数个采集周期内不再增长。
        await asyncio.sleep(1.0)
        settled = len(read_jsonl(sink_path))
        await asyncio.sleep(0.8)
        assert len(read_jsonl(sink_path)) == settled

        # ---- 主进程存活：控制面正常应答，读失败以 RPC 错误呈现 ----
        assert proc.is_running()
        status = await ctl_status(proc)
        assert status is not None, "ctl status RPC failed while collector should be alive"
        assert status["running"] is True
        read = await run_ctl_async(
            "read", "modbus-1", "rotor.speed", target=proc.grpc_target
        )
        assert read.returncode == 2
        assert "RPC failed" in read.stderr

        # ---- 恢复：从站重启，驱动自动重连 ----
        await modbus_server.start()
        await wait_status(
            proc,
            lambda p: p["devices_connected"] == 1,
            description="device reconnected",
        )

        # ---- 数据恢复：新点值继续到达 sink ----
        rows = await wait_file_rows(sink_path, min_rows=settled + 2)
        rotor = [r for r in rows if r["point_id"] == "rotor.speed"]
        assert rotor, "rotor.speed rows missing after recovery"
        assert all(r["value"] == pytest.approx(1200.5) for r in rotor)

        # 恢复后即时读也恢复正常。
        read = await run_ctl_async(
            "read", "modbus-1", "rotor.speed", target=proc.grpc_target
        )
        assert read.returncode == 0
