"""Collector 在领域快照边界校验 Task output 点引用和 Modbus 地址。"""

from dataclasses import replace
from pathlib import Path

import pytest

from collector.infrastructure.config.loader import collector_config_from_snapshot
from core.application.errors import ConfigError
from core.domain import Sink
from core.domain.task import TaskOutput
from core.infrastructure.config.adapter import YamlConfigAdapter
from tests.support.new_collector import write_collector_config_tree

pytestmark = pytest.mark.unit


def _snapshot_with_outputs(tmp_path: Path, *, addresses: tuple[int, ...]):
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
    )
    task = snapshot.tasks["t1"]
    task = replace(
        task,
        sink_ids=("test_modbus",),
        outputs=(TaskOutput(sink_id="test_modbus", points=points),),
    )
    return replace(
        snapshot,
        sinks={**snapshot.sinks, sink.sink_id: sink},
        tasks={"t1": task},
    )


def test_modbus_sink_resolves_point_from_snapshot(tmp_path: Path) -> None:
    config = collector_config_from_snapshot(_snapshot_with_outputs(tmp_path, addresses=(100,)))
    point = config.sinks["test_modbus"].points[0]
    assert point["source"]["device_id"]
    assert point["source_data_type"]
    assert point["datatype"] == point["source_data_type"]


def test_modbus_sink_rejects_overlapping_registers(tmp_path: Path) -> None:
    snapshot = _snapshot_with_outputs(tmp_path, addresses=(100, 100))
    with pytest.raises(ConfigError, match="overlapping"):
        collector_config_from_snapshot(snapshot)


def test_modbus_sink_rejects_missing_source_device(tmp_path: Path) -> None:
    snapshot = _snapshot_with_outputs(tmp_path, addresses=(100,))
    task = snapshot.tasks["t1"]
    output = task.outputs[0]
    point = {**output.points[0], "source": {"device_id": "unknown", "point_id": "p"}}
    modified_output = TaskOutput(sink_id=output.sink_id, points=(point,))
    modified_task = replace(task, outputs=(modified_output,))
    with pytest.raises(ConfigError, match="unknown device"):
        collector_config_from_snapshot(replace(snapshot, tasks={"t1": modified_task}))


def test_disabled_task_outputs_are_not_aggregated_into_sinks(tmp_path: Path) -> None:
    """禁用 Task 不部署：其点不得占用寄存器/白名单，也不得触发重叠拒绝。"""
    snapshot = _snapshot_with_outputs(tmp_path, addresses=(100,))
    enabled_task = snapshot.tasks["t1"]
    # 与启用 Task 完全同址的输出——启用状态必被重叠拒绝，禁用必须被忽略。
    disabled_task = replace(
        enabled_task,
        task_id="t2",
        enabled=False,
        sink_ids=("test_modbus",),
        outputs=(
            TaskOutput(
                sink_id="test_modbus",
                points=(
                    {
                        "source": enabled_task.outputs[0].points[0]["source"],
                        "ref": "ghost",
                        "address": {
                            "slave_id": 1,
                            "register_type": "holding",
                            "address": 100,
                        },
                    },
                ),
            ),
        ),
    )
    config = collector_config_from_snapshot(
        replace(snapshot, tasks={"t1": enabled_task, "t2": disabled_task})
    )
    sink = config.sinks["test_modbus"]
    assert {point["ref"] for point in sink.points} == {"scada_0"}
    assert set(sink.task_sources) == {"t1"}


def test_disabled_task_with_stale_device_does_not_block_startup(tmp_path: Path) -> None:
    """禁用 Task 的过期设备引用不得阻断 Collector 启动。"""
    snapshot = _snapshot_with_outputs(tmp_path, addresses=(100,))
    enabled_task = snapshot.tasks["t1"]
    stale_output = TaskOutput(
        sink_id="test_modbus",
        points=(
            {
                "source": {"device_id": "decommissioned", "point_id": "p"},
                "ref": "stale",
                "address": {
                    "slave_id": 1,
                    "register_type": "holding",
                    "address": 200,
                },
            },
        ),
    )
    disabled_task = replace(
        enabled_task,
        task_id="t2",
        enabled=False,
        sink_ids=("test_modbus",),
        outputs=(stale_output,),
    )
    config = collector_config_from_snapshot(
        replace(snapshot, tasks={"t1": enabled_task, "t2": disabled_task})
    )
    assert set(config.sinks["test_modbus"].task_sources) == {"t1"}
