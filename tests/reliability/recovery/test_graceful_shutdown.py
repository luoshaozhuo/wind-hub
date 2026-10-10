"""Recovery：优雅停机验收——SIGTERM 后已确认写入的数据必须保留、进程干净退出。

当前 File sink 无应用层缓冲：每个 write 批次落盘并 flush 后才返回，
关闭语义是 close() 释放文件句柄。因此优雅停机的边界证据是：SIGTERM
前已确认写入的行在停机后完整保留、CSV 可解析无截断、进程按退出码 0
干净退出。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.reliability.recovery.helpers import wait_status, write_modbus_file_config
from tests.support.control import apply_placement_and_start_instance
from tests.support.process import CollectorProcess
from tests.support.wait import read_csv, wait_file_rows

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"


class TestGracefulShutdown:
    async def test_sigterm_closes_file_sink_without_data_loss(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        sink_path = tmp_path / "out" / "telemetry.csv"
        config_dir = write_modbus_file_config(
            tmp_path / "cfg",
            modbus_server.port,
            sink_path,
        )
        proc: CollectorProcess = await collector_factory(config_dir, shutdown_timeout=15.0)

        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
        # File sink 每批次 write 落盘并 flush 后才返回——文件里可见的行
        # 即「已确认写入」的记录。
        await wait_file_rows(sink_path, min_rows=4)
        written_rows = read_csv(sink_path)

        # ---- SIGTERM：优雅停机必须干净退出且不丢已确认写入 ----
        exit_code = proc.terminate(timeout=30.0)
        assert exit_code == 0

        rows = read_csv(sink_path)
        # 停机前已确认写入的记录全部保留（停机窗口内允许追加，不允许丢失）。
        assert len(rows) >= len(written_rows)
        point_ids = {r["point_id"] for r in rows}
        assert {"rotor.speed", "gen.power", "temp.int", "setpoint.power"} <= point_ids
        assert all(r["device_id"] == "modbus-1" for r in rows)
        # 文件完整：最后一行以换行结尾，无写一半的截断行。
        assert sink_path.read_bytes().endswith(b"\n")

    async def test_sigterm_during_device_outage_still_exits_cleanly(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        tmp_path: Path,
    ) -> None:
        """设备断连状态下的 SIGTERM：停机不能被卡死的重连/读超时拖住。"""
        sink_path = tmp_path / "out" / "telemetry.csv"
        config_dir = write_modbus_file_config(tmp_path / "cfg", modbus_server.port, sink_path)
        proc: CollectorProcess = await collector_factory(config_dir, shutdown_timeout=15.0)

        await apply_placement_and_start_instance(
            proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
        )
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
