"""端到端测试 —— 完整运行时 + DBSink（数据库以假连接池替代）。

驱动一条真实链路：Modbus 模拟从站 → 采集 → 路由 → DBSink 批量写入。由于本环境
无真实 PostgreSQL（无需 Docker），``asyncpg`` 替换为内存假实现（决策 7），验证
full runtime 下点值被映射成行（``value`` 以 JSON 文本落库）并写入配置的表；
采集/路由/调度均为真实路径。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.fixtures.servers.modbus_server import ModbusMockServer
from wind_hub.assembly import assemble, start_runtime, stop_runtime

_MODULE = "wind_hub.adapter.outbound.sink.db.postgres"
_PORT = 15031


class _CapturingPool:
    """内存假连接池：积累所有 executemany 写入的行，供断言检查。"""

    instances: list[_CapturingPool] = []

    def __init__(self, dsn: str, **kwargs: Any) -> None:
        self.dsn = dsn
        self.executemany_calls: list[tuple[str, list[tuple[object, ...]]]] = []
        _CapturingPool.instances.append(self)

    async def execute(self, sql: str) -> None:
        return None

    async def executemany(self, sql: str, rows: list[tuple[object, ...]]) -> None:
        self.executemany_calls.append((sql, list(rows)))

    async def close(self) -> None:
        return None


class _FakeAsyncpg:
    """替代 ``asyncpg`` 模块：只暴露本 sink 用到的 ``create_pool``。"""

    async def create_pool(self, dsn: str, min_size: int, max_size: int) -> _CapturingPool:
        return _CapturingPool(dsn, min_size=min_size, max_size=max_size)


def _write_yaml(dir_path: Path, name: str, data: dict[str, Any]) -> None:
    with open(dir_path / name, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)


def _write_config(tmp_path: Path) -> Path:
    """写一份最小完整配置：Modbus 设备 + DB sink + 两个点 + 默认路由。"""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir()
    _write_yaml(
        cfg_dir,
        "system.yaml",
        {
            "scheduler": {
                "default_interval": 0.2,
                "connect_timeout": 2.0,
                "read_timeout": 2.0,
                "shutdown_timeout": 5.0,
            },
            "pipeline": {"processors": []},
            "sinks": [
                {
                    "name": "db",
                    "type": "db",
                    "params": {"dsn": "postgresql://u:p@localhost/windhub", "table": "points"},
                },
            ],
        },
    )
    _write_yaml(
        cfg_dir,
        "devices.yaml",
        {
            "devices": [
                {
                    "device_id": "modbus-1",
                    "protocol": "modbus",
                    "endpoint": {
                        "host": "127.0.0.1",
                        "port": _PORT,
                        "extensions": {
                            "unit_id": 1,
                            "timeout": 2.0,
                            "reconnect_max_retries": 20,
                            "reconnect_backoff_max": 1.0,
                        },
                    },
                    "polling": [{"group": "telemetry", "interval": 0.2}],
                }
            ],
        },
    )
    _write_yaml(
        cfg_dir,
        "points.yaml",
        {
            "points": [
                {
                    "point_id": "rotor.speed",
                    "device_id": "modbus-1",
                    "address": {"register_type": "holding", "address": 100},
                    "data_type": "float32",
                },
                {
                    "point_id": "gen.power",
                    "device_id": "modbus-1",
                    "address": {"register_type": "holding", "address": 102},
                    "data_type": "float32",
                },
            ],
        },
    )
    _write_yaml(
        cfg_dir,
        "routing.yaml",
        {"rules": [{"name": "default", "targets": ["db"], "priority": 0}]},
    )
    return cfg_dir


async def _wait_for(predicate: Callable[[], Any], timeout: float = 10.0) -> None:
    """轮询直到 ``predicate()`` 返回真值，否则超时失败。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture
def fake_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    _CapturingPool.instances = []
    monkeypatch.setattr(f"{_MODULE}.asyncpg", _FakeAsyncpg())


@pytest.fixture
async def server() -> Iterator[ModbusMockServer]:
    s = ModbusMockServer(port=_PORT)
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


def _rows_for(point_id: str) -> list[tuple[object, ...]]:
    """从所有假连接池实例里筛出指定 point_id 的行。"""
    out: list[tuple[object, ...]] = []
    for pool in _CapturingPool.instances:
        for _sql, rows in pool.executemany_calls:
            out.extend(row for row in rows if row[1] == point_id)
    return out


async def test_db_sink_full_runtime(
    tmp_path: Path, server: ModbusMockServer, fake_asyncpg: None
) -> None:
    """完整运行时写入（假）PostgreSQL：行字段正确、value 为 JSON 文本。"""
    cfg_dir = _write_config(tmp_path)
    rt = assemble(cfg_dir)
    await start_runtime(rt)
    try:
        await _wait_for(lambda: _rows_for("rotor.speed"))
    finally:
        await stop_runtime(rt)

    rows = _rows_for("rotor.speed")
    assert rows, "没有 rotor.speed 行写入数据库"

    device_id, point_id, value_json, quality, timestamp, source = rows[0]
    assert device_id == "modbus-1"
    assert point_id == "rotor.speed"
    assert json.loads(str(value_json)) == pytest.approx(1200.5)
    assert quality == "good"
    assert source == "modbus"
    assert timestamp is not None
