"""Core 类型化配置读取契约测试。"""

from __future__ import annotations

import pytest

from core.application import ConfigError
from core.application.port import TypedConfigReader
from core.infrastructure.config import YamlTypedConfigAdapter


def test_typed_reader_returns_independent_validated_topics(tmp_path):
    files = {
        "device_models": "device_types: {}\ndevice_models: {}\n",
        "devices": "devices: []\n",
        "points": "point_tables: {}\n",
        "units": "units: {}\n",
        "tasks": "tasks: []\n",
        "system": "runtime:\n  connect_timeout: 10\n",
        "sinks": "sinks: []\n",
    }
    for name, body in files.items():
        (tmp_path / f"{name}.yaml").write_text(body, encoding="utf-8")

    reader = YamlTypedConfigAdapter(tmp_path)
    assert isinstance(reader, TypedConfigReader)
    assert reader.read_device_config().instances.devices == []
    assert reader.read_device_config().models.device_models == {}
    assert reader.read_point_config().tables == {}
    assert reader.read_task_config().tasks == ()
    assert reader.read_unit_config().units == {}
    assert reader.read_sink_config().sinks == []
    assert reader.read_system_config().sections["runtime"]["connect_timeout"] == 10


def test_invalid_task_is_rejected_without_loading_other_topics(tmp_path):
    (tmp_path / "tasks.yaml").write_text(
        "tasks:\n"
        "  - task_id: bad\n"
        "    device: wt01\n"
        "    device_group: wind\n"
        "    point_group: fast\n"
        "    targets: [{sink: archive}]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="exactly one"):
        YamlTypedConfigAdapter(tmp_path).read_task_config()


def test_point_config_expands_inheritance(tmp_path):
    (tmp_path / "points.yaml").write_text(
        "point_tables:\n"
        "  base:\n"
        "    protocol: modbus\n"
        "    points:\n"
        "      - point_id: power\n"
        "        point_groups: [fast]\n"
        "        address: {type: holding, offset: 0}\n"
        "  child:\n"
        "    extends: base\n",
        encoding="utf-8",
    )
    points = YamlTypedConfigAdapter(tmp_path).read_point_config()
    assert "power" in points.tables["child"].points


def test_invalid_sink_is_rejected_without_other_topics(tmp_path):
    (tmp_path / "sinks.yaml").write_text(
        "sinks:\n  - name: bad\n    type: invalid\n    connection: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        YamlTypedConfigAdapter(tmp_path).read_sink_config()


def test_system_config_is_deeply_immutable(tmp_path):
    (tmp_path / "system.yaml").write_text(
        "runtime:\n  nested:\n    values: [1, 2]\n",
        encoding="utf-8",
    )
    config = YamlTypedConfigAdapter(tmp_path).read_system_config()
    with pytest.raises(TypeError):
        config.sections["runtime"]["nested"] = {}
    with pytest.raises(TypeError):
        config.sections["runtime"]["nested"]["values"][0] = 10


def test_resolved_point_table_index_is_read_only(tmp_path):
    (tmp_path / "points.yaml").write_text(
        "point_tables:\n"
        "  main:\n"
        "    protocol: ads\n"
        "    points:\n"
        "      - point_id: speed\n"
        "        point_groups: [fast]\n"
        "        address: {type: MAIN.speed}\n",
        encoding="utf-8",
    )
    config = YamlTypedConfigAdapter(tmp_path).read_point_config()
    with pytest.raises(TypeError):
        config.tables["main"].points["speed"] = None


def test_device_models_and_instances_are_independently_readable(tmp_path):
    (tmp_path / "device_models.yaml").write_text(
        "device_types: {}\ndevice_models: {}\n",
        encoding="utf-8",
    )
    reader = YamlTypedConfigAdapter(tmp_path)
    assert reader.read_device_models_config().definition.device_models == {}
    with pytest.raises(ConfigError, match="not found"):
        reader.read_device_instances_config()


def test_task_config_returns_immutable_definitions(tmp_path):
    (tmp_path / "tasks.yaml").write_text(
        "tasks:\n"
        "  - task_id: sample\n"
        "    device: wt01\n"
        "    point_group: fast\n"
        "    targets: [{sink: archive}]\n",
        encoding="utf-8",
    )
    tasks = YamlTypedConfigAdapter(tmp_path).read_task_config().tasks
    assert tasks[0].targets == ("archive",)
    with pytest.raises(AttributeError):
        tasks[0].task_id = "changed"


def test_units_are_independent_and_immutable(tmp_path):
    (tmp_path / "units.yaml").write_text(
        "units:\n  none:\n    symbol: ''\n    name: Dimensionless\n",
        encoding="utf-8",
    )
    result = YamlTypedConfigAdapter(tmp_path).read_unit_config()
    assert result.units["none"].name == "Dimensionless"
    with pytest.raises(TypeError):
        result.units["new"] = None
    with pytest.raises(AttributeError):
        result.units["none"].symbol = "changed"
