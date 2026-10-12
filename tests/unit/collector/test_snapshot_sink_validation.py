"""Collector 在领域快照边界校验 Sink 点引用和 Modbus 地址。"""

from dataclasses import replace
from pathlib import Path

import pytest

from collector.infrastructure.config.loader import _from_snapshot
from core.application.errors import ConfigError
from core.domain import Sink
from core.infrastructure.config.adapter import YamlConfigAdapter
from tests.support.new_collector import write_collector_config_tree


def _snapshot_with_points(tmp_path: Path, *, addresses: tuple[int, ...]):
    snapshot = YamlConfigAdapter(write_collector_config_tree(tmp_path / "cfg")).load()
    device_id, device = next(iter(snapshot.site.devices.items()))
    model = snapshot.device_models[device.device_model_id]
    table = snapshot.point_tables[model.point_table_id]
    point_id = next(iter(table.points))
    points = tuple(
        {
            "source": {"device_id": str(device_id), "point_id": point_id},
            "ref": f"scada_{index}",
            "address": {
                "slave_id": 1,
                "register_type": "holding",
                "address": address,
            },
        }
        for index, address in enumerate(addresses)
    )
    sink = Sink(
        sink_id="test_modbus",
        kind="modbus",
        enabled=True,
        connection={"host": "127.0.0.1", "port": 1502},
        points=points,
    )
    return replace(snapshot, sinks={**snapshot.sinks, sink.sink_id: sink})


def test_modbus_sink_resolves_point_from_snapshot(tmp_path: Path) -> None:
    config = _from_snapshot(_snapshot_with_points(tmp_path, addresses=(100,)))
    point = config.sinks["test_modbus"].points[0]
    assert point["source"]["device_id"]
    assert point["source_data_type"]
    assert point["datatype"] == point["source_data_type"]


def test_modbus_sink_rejects_overlapping_registers(tmp_path: Path) -> None:
    snapshot = _snapshot_with_points(tmp_path, addresses=(100, 100))
    with pytest.raises(ConfigError, match="overlapping"):
        _from_snapshot(snapshot)


def test_modbus_sink_rejects_missing_source_device(tmp_path: Path) -> None:
    snapshot = _snapshot_with_points(tmp_path, addresses=(100,))
    sink = snapshot.sinks["test_modbus"]
    point = {**sink.points[0], "source": {"device_id": "unknown", "point_id": "p"}}
    modified = replace(sink, points=(point,))
    with pytest.raises(ConfigError, match="unknown device"):
        _from_snapshot(replace(snapshot, sinks={**snapshot.sinks, sink.sink_id: modified}))
