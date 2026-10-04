"""端到端测试 —— 完整运行时 + 真实 FileSink 落盘。

驱动一条真实链路：Modbus 模拟从站 → 采集 → Task 分发 → 真实 FileSink 写入本地文件。
验证 ``jsonl`` 与 ``csv`` 两种格式的落盘内容，以及优雅停机后文件依然完整
（``close`` 强制 flush，无半行残缺）。

与 ``tests/integration/e2e`` 的 null-sink 测试不同，这里**不注入** sink 工厂，
走 ``assemble()`` 默认的 ``_create_sink``，让 FileSink 真实落到 ``tmp_path``。
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.config_helper import write_config_tree
from wind_hub_collector.assembly import AssembledRuntime, assemble, start_runtime, stop_runtime

_CSV_HEADER = ["device_id", "point_id", "value", "quality", "timestamp", "source"]


def _write_yaml(dir_path: Path, name: str, data: dict[str, Any]) -> None:
    with open(dir_path / name, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)


def _write_config(tmp_path: Path, sink_path: Path, **sink_params: Any) -> Path:
    """写一份最小完整配置树（common/ + site/）：1 台 modbus 设备 + 1 个
    file sink + 2 个点 + 1 个采集 Task。返回 site 配置目录。"""
    return write_config_tree(
        tmp_path,
        devices=[
            {
                "device_id": "modbus-1",
                "protocol": "modbus",
                "point_table": "modbus",
                "endpoint": {
                    "host": "127.0.0.1",
                    "port": 15020,
                    "extensions": {
                        "unit_id": 1,
                        "timeout": 2.0,
                        "reconnect_max_retries": 20,
                        "reconnect_backoff_max": 1.0,
                        # mock server 寄存器布局为 big-endian float32
                        "word_order": "big_endian",
                    },
                },
            }
        ],
        point_tables={
            "modbus": {
                "points": [
                    {
                        "point_id": "rotor.speed",
                        "point_groups": ["telemetry"],
                        "address": {"register_type": "holding", "address": 100},
                        "data_type": "float32",
                    },
                    {
                        "point_id": "gen.power",
                        "point_groups": ["telemetry"],
                        "address": {"register_type": "holding", "address": 102},
                        "data_type": "float32",
                    },
                ],
            },
        },
        sinks=[
            {"name": "file", "type": "file", "connection": {"path": str(sink_path), **sink_params}},
        ],
        tasks=[
            {
                "task_id": "modbus-telemetry",
                "device": "modbus-1",
                "point_group": "telemetry",
                "interval": 0.2,
                "targets": [{"sink": "file"}],
            }
        ],
        system={
            "runtime": {"connect_timeout": 2.0, "read_timeout": 2.0, "shutdown_timeout": 5.0}
        },
    )


def _jsonl_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().split("\n") if line]


def _csv_lines(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return list(csv.reader(io.StringIO(path.read_text())))


async def _wait_for(predicate: Callable[[], Any], timeout: float = 10.0) -> None:
    """轮询直到 ``predicate()`` 返回真值，否则超时失败。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture
async def server() -> Iterator[ModbusMockServer]:
    s = ModbusMockServer()
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


async def test_file_sink_jsonl_full_runtime(tmp_path: Path, server: ModbusMockServer) -> None:
    """jsonl 落盘：采集值正确、字段完整，停机后文件依然完整。"""
    sink_path = tmp_path / "data" / "archive.jsonl"
    cfg_dir = _write_config(tmp_path, sink_path, buffer_size=1)
    rt = assemble(cfg_dir)
    await start_runtime(rt)
    for instance in rt.runtime.task_instances().values():
        await rt.runtime.start_task_instance(instance.instance_id)
    try:

        def _find(point_id: str) -> dict[str, Any] | None:
            for line in _jsonl_lines(sink_path):
                if line["point_id"] == point_id:
                    return line
            return None

        await _wait_for(lambda: _find("rotor.speed"))
        await _wait_for(lambda: _find("gen.power"))

        rotor = _find("rotor.speed")
        assert rotor is not None
        assert rotor["value"] == pytest.approx(1200.5)
        assert rotor["source"] == "modbus"
        assert rotor["device_id"] == "modbus-1"
        assert rotor["quality"] == "good"
        assert rotor["timestamp"].endswith("+00:00")

        gen = _find("gen.power")
        assert gen is not None
        assert gen["value"] == pytest.approx(800.0)
    finally:
        await stop_runtime(rt)

    # 停机后文件仍完整可解析，且两个点都在（close 强制 flush，无半行）。
    final_lines = _jsonl_lines(sink_path)
    assert {line["point_id"] for line in final_lines} >= {"rotor.speed", "gen.power"}


async def test_file_sink_csv_full_runtime(tmp_path: Path, server: ModbusMockServer) -> None:
    """csv 落盘：表头正确、行字段正确，停机后文件完整。"""
    sink_path = tmp_path / "archive.csv"
    cfg_dir = _write_config(tmp_path, sink_path, format="csv", buffer_size=1)
    rt: AssembledRuntime = assemble(cfg_dir)
    await start_runtime(rt)
    for instance in rt.runtime.task_instances().values():
        await rt.runtime.start_task_instance(instance.instance_id)
    try:

        def _rows() -> list[list[str]]:
            return _csv_lines(sink_path)[1:]  # 去掉表头

        await _wait_for(lambda: any("rotor.speed" in row for row in _rows()))
    finally:
        await stop_runtime(rt)

    lines = _csv_lines(sink_path)
    assert lines[0] == _CSV_HEADER
    assert len(lines) > 1
    rotor_rows = [row for row in lines[1:] if row[1] == "rotor.speed"]
    assert rotor_rows
    assert float(rotor_rows[0][2]) == pytest.approx(1200.5)
    assert rotor_rows[0][3] == "good"
