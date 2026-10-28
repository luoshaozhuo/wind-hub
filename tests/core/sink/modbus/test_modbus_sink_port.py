"""ModbusSink（SinkPort 装配层）契约。

- 构造：必须 ResolvedSink 且地址必须 ModbusSinkAddress；
- write/write_task：批次经数据路径落库，任务白名单隔离；
- 生命周期：exclusive_open、health 未监听/监听两态、open/close 真实回环。
"""

from __future__ import annotations

import socket

import pytest

from core.application import ConfigError, Quality
from core.application.sink_contract import ResolvedSink
from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.sink.modbus.sink import ModbusSink

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]


def _point_dict(ref: str, register_type: str = "holding", address: int = 0):
    return {
        "source": {"device_id": "wt01", "point_id": ref},
        "ref": ref,
        "source_data_type": "uint16",
        "source_unit": "none",
        "datatype": "uint16",
        "unit": "none",
        "address": {
            "slave_id": 1,
            "register_type": register_type,
            "address": address,
        },
    }


def _resolved(**overrides) -> ResolvedSink:
    values = {
        "sink_id": "modbus-sink-1",
        "kind": "modbus",
        "enabled": True,
        "connection": {"host": "127.0.0.1", "port": 1502},
        "points": (_point_dict("scada.a", address=10), _point_dict("scada.b", address=11)),
        "task_sources": {"task-a": (("wt01", "scada.a"),)},
        "task_refs": {"task-a": ("scada.a",)},
    }
    values.update(overrides)
    return ResolvedSink(**values)


def test_requires_resolved_sink() -> None:
    plain = Sink(
        sink_id="m",
        kind="modbus",
        enabled=True,
        connection={"host": "127.0.0.1", "port": 1502},
    )
    with pytest.raises(ConfigError, match="resolved Task output mapping"):
        ModbusSink(plain)


def test_rejects_non_modbus_address() -> None:
    bad = dict(_point_dict("scada.a"))
    bad["address"] = {"field": "scada.a"}
    with pytest.raises(ConfigError, match="requires ModbusSinkAddress"):
        ModbusSink(_resolved(points=(bad,)))


def test_exclusive_open_and_port() -> None:
    sink = ModbusSink(_resolved(connection={"host": "127.0.0.1", "port": 1502}))
    assert sink.exclusive_open is True
    assert sink.port == 1502


async def test_write_updates_data_path_empty_batch_is_noop() -> None:
    sink = ModbusSink(_resolved())

    await sink.write([])  # 不接触数据路径也不报错

    await sink.write(
        [PointValue(device_id="wt01", point_id="scada.a", value=0x0102, quality=Quality.GOOD)]
    )
    assert sink.data_path.store.read_registers(1, "holding", 10) == [0x0102]
    await sink.flush()  # 内存 Sink 无 flush 动作


async def test_write_task_isolated_by_task_whitelist() -> None:
    sink = ModbusSink(_resolved())
    batch = [
        PointValue(device_id="wt01", point_id="scada.a", value=1, quality=Quality.GOOD),
        PointValue(device_id="wt01", point_id="scada.b", value=2, quality=Quality.GOOD),
    ]

    await sink.write_task("task-a", batch)
    await sink.write_task("task-unknown", batch)

    assert sink.data_path.store.read_registers(1, "holding", 10) == [1]
    # scada.b 不属于 task-a 声明映射，未知任务完全不写。
    assert sink.data_path.store.read_registers(1, "holding", 11) == [0]


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


async def test_open_close_lifecycle() -> None:
    port = _free_port()
    sink = ModbusSink(_resolved(connection={"host": "127.0.0.1", "port": port}))

    assert (await sink.health()).healthy is False
    await sink.open()
    assert (await sink.health()).healthy is True
    await sink.close()
    assert (await sink.health()).healthy is False
