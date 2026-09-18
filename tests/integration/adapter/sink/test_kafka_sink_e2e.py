"""端到端测试 —— 完整运行时 + KafkaSink（broker 以假生产者替代）。

驱动一条真实链路：Modbus 模拟从站 → 采集 → 路由 → KafkaSink 投递。由于本环境无
真实 Kafka broker（无需 Docker），``AIOKafkaProducer`` 替换为内存假实现
（决策 7），验证 full runtime 下点值被序列化成 UTF-8 JSON、带正确 key 投递到
配置的主题；采集/路由/调度均为真实路径。
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

_MODULE = "wind_hub.adapter.outbound.sink.mq.kafka"
_PORT = 15030


class _CapturingProducer:
    """内存假生产者：积累所有投递消息，供断言检查。"""

    instances: list[_CapturingProducer] = []

    def __init__(self, **kwargs: Any) -> None:
        self.sent: list[tuple[str, bytes, bytes | None]] = []
        _CapturingProducer.instances.append(self)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def send(self, topic: str, value: bytes, key: bytes | None) -> None:
        self.sent.append((topic, value, key))


def _write_yaml(dir_path: Path, name: str, data: dict[str, Any]) -> None:
    with open(dir_path / name, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)


def _write_config(tmp_path: Path, **sink_params: Any) -> Path:
    """写一份最小完整配置：Modbus 设备 + Kafka sink + 两个点 + 默认路由。"""
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
                    "name": "kafka",
                    "type": "kafka",
                    "params": {
                        "bootstrap_servers": "localhost:9092",
                        "topic": "wind-hub.raw",
                        **sink_params,
                    },
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
        {"rules": [{"name": "default", "targets": ["kafka"], "priority": 0}]},
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
def fake_producer(monkeypatch: pytest.MonkeyPatch) -> None:
    _CapturingProducer.instances = []
    monkeypatch.setattr(f"{_MODULE}.AIOKafkaProducer", _CapturingProducer)


@pytest.fixture
async def server() -> Iterator[ModbusMockServer]:
    s = ModbusMockServer(port=_PORT)
    await s.start()
    try:
        yield s
    finally:
        await s.stop()


async def test_kafka_sink_full_runtime(
    tmp_path: Path, server: ModbusMockServer, fake_producer: None
) -> None:
    """完整运行时投递到（假）Kafka：主题、JSON 内容、分区 key 均正确。"""
    cfg_dir = _write_config(tmp_path, key_field="device_id")
    rt = assemble(cfg_dir)
    await start_runtime(rt)
    try:
        await _wait_for(lambda: _tagged_msgs("rotor.speed"))
    finally:
        await stop_runtime(rt)

    rotor_msgs = _tagged_msgs("rotor.speed")
    assert rotor_msgs, "没有 rotor.speed 消息投递到 Kafka"

    topic, value, key = rotor_msgs[0]
    assert topic == "wind-hub.raw"
    assert key == b"modbus-1"  # key_field=device_id
    data = json.loads(value.decode("utf-8"))
    assert data["device_id"] == "modbus-1"
    assert data["point_id"] == "rotor.speed"
    assert data["value"] == pytest.approx(1200.5)
    assert data["quality"] == "good"
    assert data["source"] == "modbus"
    assert data["timestamp"].endswith("+00:00")


def _tagged_msgs(point_id: str) -> list[tuple[str, bytes, bytes | None]]:
    """从所有假生产者实例里筛出指定 point_id 的消息。"""
    out: list[tuple[str, bytes, bytes | None]] = []
    for producer in _CapturingProducer.instances:
        for topic, value, key in producer.sent:
            data = json.loads(value.decode("utf-8"))
            if data["point_id"] == point_id:
                out.append((topic, value, key))
    return out
