"""Recovery：优雅停机验收——SIGTERM 后缓冲数据必须落盘、进程干净退出。

File sink 配置成超大缓冲 + 超长间隔：运行期间数据只进缓冲区（文件为空），
唯一落盘机会是停机时的 close() flush。SIGTERM 后文件出现完整行，即证明
停机路径真实执行了 sink flush/close——这是系统边界可观测的优雅停机证据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.recovery.helpers import wait_status, write_modbus_file_config
from tests.system.process import CollectorProcess, run_ctl_async
from tests.system.wait import read_jsonl

pytestmark = pytest.mark.modbus

INSTANCE_ID = "modbus-telemetry:modbus-1"


class TestGracefulShutdown:
    async def test_sigterm_flushes_buffered_points_and_exits_cleanly(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(
            tmp_path / "cfg",
            modbus_server.port,
            sink_path,
            buffer_size=100000,
            flush_interval=3600.0,
        )
        proc: CollectorProcess = await collector_factory(config_dir, shutdown_timeout=15.0)

        assert (
            await run_ctl_async("start-instance", INSTANCE_ID, target=proc.grpc_target)
        ).ok
        # 采集确已发生（计数来自 ctl status 的边界观测）……
        await wait_status(
            proc,
            lambda p: p["points_collected"] >= 4,
            description="points collected before shutdown",
        )
        # ……但缓冲未落盘：文件不存在或为空。
        assert not sink_path.exists() or not read_jsonl(sink_path)

        # ---- SIGTERM：优雅停机必须 flush 缓冲并干净退出 ----
        exit_code = proc.terminate(timeout=30.0)
        assert exit_code == 0

        rows = read_jsonl(sink_path)
        assert len(rows) >= 4
        point_ids = {r["point_id"] for r in rows}
        assert {"rotor.speed", "gen.power", "temp.int", "setpoint.power"} <= point_ids
        assert all(r["device_id"] == "modbus-1" for r in rows)

    async def test_sigterm_during_device_outage_still_exits_cleanly(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        """设备断连状态下的 SIGTERM：停机不能被卡死的重连/读超时拖住。"""
        sink_path = tmp_path / "out" / "telemetry.jsonl"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir, shutdown_timeout=15.0)

        assert (
            await run_ctl_async("start-instance", INSTANCE_ID, target=proc.grpc_target)
        ).ok
        await wait_status(
            proc,
            lambda p: p["points_collected"] >= 1,
            description="collection started",
        )

        # 故障注入后直接 SIGTERM——不停 server、不做恢复。
        await modbus_server.stop()
        await wait_status(
            proc,
            lambda p: p["devices_connected"] == 0,
            description="device detected disconnected",
        )

        exit_code = proc.terminate(timeout=30.0)
        assert exit_code == 0
