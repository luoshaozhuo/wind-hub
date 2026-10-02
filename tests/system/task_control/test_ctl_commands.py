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

from tests.component.collector.conftest import write_functional_config
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.process import CollectorProcess, run_ctl_async

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

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
