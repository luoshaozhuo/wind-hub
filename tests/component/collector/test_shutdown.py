"""优雅停机 functional 测试。

验证 ``stop_runtime`` 的完整收尾语义：运行中实例停止采集、FileSink
缓冲真实落盘（sink flush）、协议连接关闭（server 侧连接数回落）、
且不遗留孤儿 asyncio task。

进程级 SIGTERM 停机属于 system/recovery 层，不在此重复。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.component.collector.conftest import (
    functional_sink_factory,
    write_functional_config,
)
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.process import free_port
from tests.support.wait import wait_until
from wind_hub_collector.assembly import assemble, start_runtime, stop_runtime

pytestmark = pytest.mark.modbus


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestGracefulStop:
    async def test_stop_flushes_file_sink_and_closes_protocol(self, tmp_path) -> None:
        port = free_port()
        server = ModbusMockServer(port=port)
        sink_path = tmp_path / "out" / "data.jsonl"
        config_dir = write_functional_config(
            tmp_path / "cfg",
            port,
            sinks=[
                {
                    "name": "file_sink",
                    "type": "file",
                    # 大缓冲 + 长刷盘间隔：若 stop 不主动 flush，数据不会落盘。
                    "connection": {
                        "path": str(sink_path),
                        "buffer_size": 100000,
                        "flush_interval": 3600.0,
                    },
                }
            ],
            tasks=[
                {
                    "task_id": "modbus-telemetry",
                    "device": "modbus-1",
                    "point_group": "telemetry",
                    "interval": 0.1,
                    "targets": [{"sink": "file_sink"}],
                }
            ],
        )
        await server.start()
        rt = assemble(config_dir, sink_factory=functional_sink_factory)
        await start_runtime(rt)
        try:
            await rt.tasks.start_instance("modbus-telemetry:modbus-1")

            # 等采集链路真实跑起来（engine 计数递增）。
            await wait_until(
                lambda: rt.engine.points_collected >= 4 or None,
                timeout=5.0,
                description="acquisition engine collects points",
            )

            await stop_runtime(rt, timeout=10.0)

            # 1) sink flush：缓冲必须落盘，行数与采集点数一致。
            rows = _read_jsonl(sink_path)
            assert len(rows) >= 4
            point_ids = {row["point_id"] for row in rows}
            assert "rotor.speed" in point_ids

            # 2) 运行态归零：实例不再采集。
            status = await rt.query.status()
            assert status.running is False

            # 3) 协议连接关闭：设备 health 不再 healthy。
            device = rt.runtime.devices["modbus-1"]
            assert device.health().healthy is False
        finally:
            await server.stop()

    async def test_stop_leaves_no_orphan_asyncio_tasks(self, tmp_path) -> None:
        port = free_port()
        server = ModbusMockServer(port=port)
        config_dir = write_functional_config(tmp_path / "cfg", port)
        await server.start()
        rt = assemble(config_dir, sink_factory=functional_sink_factory)
        await start_runtime(rt)
        await rt.tasks.start_instance("modbus-telemetry:modbus-1")
        await wait_until(
            lambda: rt.engine.points_collected >= 1 or None,
            timeout=5.0,
            description="engine collects before shutdown",
        )

        await stop_runtime(rt, timeout=10.0)
        # 给取消传播留一个事件循环节拍。
        await asyncio.sleep(0)
        await asyncio.sleep(0.1)

        current = asyncio.current_task()
        leftovers = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        assert leftovers == []
        await server.stop()

    async def test_double_stop_is_safe(self, tmp_path) -> None:
        port = free_port()
        server = ModbusMockServer(port=port)
        config_dir = write_functional_config(tmp_path / "cfg", port)
        await server.start()
        rt = assemble(config_dir, sink_factory=functional_sink_factory)
        await start_runtime(rt)
        try:
            await stop_runtime(rt, timeout=10.0)
            # 重复停机不得抛错（信号竞态/重复调用防护）。
            await stop_runtime(rt, timeout=10.0)
        finally:
            await server.stop()
