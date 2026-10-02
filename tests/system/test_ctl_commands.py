"""System E2E：ctl subprocess → 真实 gRPC socket → Collector subprocess 控制面验收。

两个进程均为真实 subprocess（console script）；协议侧是真实 Modbus TCP
从站。写入结果以独立 pymodbus 客户端回读从站确认（与 Collector 不共享
任何连接），采集副作用以输出文件行数佐证——全链路不触碰进程内部状态。

所有 ctl 调用经 :func:`run_ctl_async` 走独立线程：测试事件循环承载着
Modbus fixture server，阻塞式 subprocess 调用会让 Collector 的协议请求
在 ctl 执行期间全部超时。
"""

from __future__ import annotations

import json
import struct
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.collector.functional.conftest import update_yaml, write_functional_config
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.system.process import CollectorProcess, run_ctl_async
from tests.system.wait import wait_file_rows

pytestmark = pytest.mark.modbus

INSTANCE_ID = "modbus-telemetry:modbus-1"


def _decode_float32(registers: list[int]) -> float:
    """按 big-endian float32 解码两个 16 位寄存器。"""
    return struct.unpack(">f", struct.pack(">HH", registers[0], registers[1]))[0]


@pytest.fixture
async def collector(
    modbus_server: ModbusMockServer,
    collector_factory,
    tmp_path: Path,
) -> AsyncIterator[CollectorProcess]:
    """已就绪的 Collector subprocess（单 Modbus 设备 + File sink）。"""
    config_dir = write_functional_config(
        tmp_path / "cfg",
        modbus_server.port,
        sinks=[
            {
                "name": "file_sink",
                "type": "file",
                "params": {
                    "path": str(tmp_path / "out" / "telemetry.jsonl"),
                    "buffer_size": 4,
                    "flush_interval": 0.5,
                },
            }
        ],
        tasks=[
            {
                "task_id": "modbus-telemetry",
                "device": "modbus-1",
                "point_group": "telemetry",
                "interval": 0.2,
                "targets": [{"sink": "file_sink"}],
            }
        ],
    )
    yield await collector_factory(config_dir, collector_id="system-ctl")


async def _ctl(proc: CollectorProcess, *args: str) -> tuple[int, dict, str]:
    """执行 ctl 并解析 JSON stdout；返回 (退出码, payload, stderr)。"""
    result = await run_ctl_async(*args, target=proc.grpc_target)
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.returncode, payload, result.stderr


class TestQueryCommands:
    async def test_info_reports_identity(self, collector: CollectorProcess) -> None:
        rc, payload, _ = await _ctl(collector, "info")
        assert rc == 0
        assert payload["collector_id"] == "system-ctl"
        assert payload["runtime_running"] is True
        assert payload["boot_config_hash"]

    async def test_status_running(self, collector: CollectorProcess) -> None:
        rc, payload, _ = await _ctl(collector, "status")
        assert rc == 0
        assert payload["running"] is True
        assert payload["device_count"] == 1
        assert payload["devices_connected"] == 1

    async def test_devices_lists_modbus_device(self, collector: CollectorProcess) -> None:
        rc, payload, _ = await _ctl(collector, "devices")
        assert rc == 0
        items = {item["device_id"]: item for item in payload["items"]}
        assert "modbus-1" in items

    async def test_read_round_trip_from_real_server(self, collector: CollectorProcess) -> None:
        rc, payload, _ = await _ctl(collector, "read", "modbus-1", "rotor.speed")
        assert rc == 0
        assert payload["device_id"] == "modbus-1"
        assert payload["point_id"] == "rotor.speed"
        assert payload["value"] == pytest.approx(1200.5)
        assert payload["quality"] == "good"

    async def test_read_unknown_device_fails_rpc(self, collector: CollectorProcess) -> None:
        rc, _, stderr = await _ctl(collector, "read", "ghost", "rotor.speed")
        assert rc == 2
        assert "RPC failed" in stderr


class TestWriteCommand:
    async def test_write_reaches_real_server(
        self, collector: CollectorProcess, modbus_server: ModbusMockServer
    ) -> None:
        rc, payload, _ = await _ctl(collector, "write", "modbus-1", "setpoint.power", "55.5")
        assert rc == 0
        assert payload["success"] is True
        # 系统边界验证：独立客户端回读从站寄存器。
        registers = await modbus_server.read_holding(1, 200, 2)
        assert _decode_float32(registers) == pytest.approx(55.5)

    async def test_write_unknown_device_inline_failure(
        self, collector: CollectorProcess
    ) -> None:
        # 生产语义：命令失败内联进 CommandResult——RPC 成功、payload 置失败。
        rc, payload, _ = await _ctl(collector, "write", "ghost", "setpoint.power", "1.0")
        assert rc == 0
        assert payload["success"] is False
        assert "ghost" in payload["error"]


class TestTaskCommands:
    async def test_task_lifecycle_controls_collection(
        self, collector: CollectorProcess, tmp_path: Path
    ) -> None:
        sink_path = tmp_path / "out" / "telemetry.jsonl"

        rc, payload, _ = await _ctl(collector, "start-instance", INSTANCE_ID)
        assert rc == 0
        assert payload["state"] == "running"

        await wait_file_rows(sink_path, min_rows=2)

        rc, payload, _ = await _ctl(collector, "stop-instance", INSTANCE_ID)
        assert rc == 0
        assert payload["state"] == "stopped"

        rc, payload, _ = await _ctl(collector, "task-instance", INSTANCE_ID)
        assert rc == 0
        assert payload["state"] == "stopped"

    async def test_start_all_and_stop_all(self, collector: CollectorProcess) -> None:
        rc, _, _ = await _ctl(collector, "start-all")
        assert rc == 0
        rc, payload, _ = await _ctl(collector, "task-instance", INSTANCE_ID)
        assert payload["state"] == "running"

        rc, _, _ = await _ctl(collector, "stop-all")
        assert rc == 0
        rc, payload, _ = await _ctl(collector, "task-instance", INSTANCE_ID)
        assert payload["state"] == "stopped"


class TestDiagnosticCommands:
    async def test_verify_device_stages_ok(self, collector: CollectorProcess) -> None:
        rc, payload, _ = await _ctl(collector, "verify-device", "modbus-1")
        assert rc == 0
        assert payload["ok"] is True
        stages = {stage["name"]: stage for stage in payload["stages"]}
        assert stages["transport"]["ok"] is True
        assert stages["protocol"]["ok"] is True

    async def test_verify_point_applies_engineering_scale(
        self, collector: CollectorProcess
    ) -> None:
        rc, payload, _ = await _ctl(collector, "verify-point", "modbus-1", "gen.power")
        assert rc == 0
        assert payload["ok"] is True
        assert payload["raw_value"] == pytest.approx(800.0)
        assert payload["engineering_value"] == pytest.approx(1610.0)

    async def test_resolve_point_address(self, collector: CollectorProcess) -> None:
        rc, payload, _ = await _ctl(collector, "resolve-point", "modbus-1", "rotor.speed")
        assert rc == 0
        assert payload["configured_address"]["address"] == 100
        assert payload["configured_address"]["register_type"] == "holding"


class TestReloadCommand:
    async def test_reload_applies_config_and_keeps_serving(
        self, collector: CollectorProcess, tmp_path: Path
    ) -> None:
        update_yaml(
            tmp_path / "cfg",
            "tasks.yaml",
            lambda data: data["tasks"][0].update({"interval": 0.9}),
        )
        rc, payload, _ = await _ctl(collector, "reload")
        assert rc == 0
        assert payload["success"] is True

        rc, payload, _ = await _ctl(collector, "task", "modbus-telemetry")
        assert rc == 0
        assert payload["interval"] == 0.9

        # 重载后控制面与数据面继续可用。
        rc, payload, _ = await _ctl(collector, "read", "modbus-1", "rotor.speed")
        assert rc == 0
        assert payload["value"] == pytest.approx(1200.5)
