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
    assert reader.read_device_config().instances.devices == ()
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
    assert reader.read_device_models_config().device_models == {}
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


def test_device_and_point_contracts_are_deeply_immutable(tmp_path):
    (tmp_path / "device_models.yaml").write_text(
        "device_types:\n  turbine: {name: Turbine}\n"
        "device_models:\n  m1:\n    device_type: turbine\n"
        "    protocol: ads\n    point_table: main\n"
        "    connection_defaults: {port: 851}\n",
        encoding="utf-8",
    )
    (tmp_path / "devices.yaml").write_text(
        "devices:\n  - device_id: d1\n    model: m1\n"
        "    endpoint: {host: localhost, extensions: {nested: [1, 2]}}\n",
        encoding="utf-8",
    )
    (tmp_path / "points.yaml").write_text(
        "point_tables:\n  main:\n    protocol: ads\n"
        "    points:\n      - point_id: p1\n"
        "        point_groups: [fast]\n        address: {type: MAIN.p1}\n",
        encoding="utf-8",
    )
    reader = YamlTypedConfigAdapter(tmp_path)
    device = reader.read_device_config()
    points = reader.read_point_config()
    with pytest.raises(TypeError):
        device.models.device_models["m1"].connection_defaults["port"] = 852
    with pytest.raises(TypeError):
        device.instances.devices[0].endpoint.extensions["nested"][0] = 3
    with pytest.raises(AttributeError):
        points.tables["main"].points["p1"].scale = 10.0


def test_immutable_device_and_point_legacy_bridge(tmp_path):
    from core.infrastructure.config.legacy import to_raw_devices, to_raw_point_tables

    (tmp_path / "device_models.yaml").write_text(
        "device_types: {t: {}}\n"
        "device_models: {m: {device_type: t, protocol: ads, point_table: main}}\n",
        encoding="utf-8",
    )
    (tmp_path / "devices.yaml").write_text(
        "devices: [{device_id: d, model: m, endpoint: {host: localhost, port: 851}}]\n",
        encoding="utf-8",
    )
    (tmp_path / "points.yaml").write_text(
        "point_tables:\n  main:\n    protocol: ads\n"
        "    points: [{point_id: p, point_groups: [fast], address: {type: MAIN.p}}]\n",
        encoding="utf-8",
    )
    reader = YamlTypedConfigAdapter(tmp_path)
    models, instances = to_raw_devices(reader.read_device_config())
    tables = to_raw_point_tables(reader.read_point_config())
    assert models.device_models["m"].point_table == "main"
    assert instances.devices[0].endpoint.port == 851
    assert tables["main"].points["p"].point_groups == ["fast"]


@pytest.mark.parametrize("section", ["ads", "runtime"])
def test_system_config_rejects_non_mapping_sections(tmp_path, section):
    (tmp_path / "system.yaml").write_text(
        f"{section}: invalid\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must be a mapping"):
        YamlTypedConfigAdapter(tmp_path).read_system_config()


def test_system_config_validates_ads_identity(tmp_path):
    (tmp_path / "system.yaml").write_text(
        "ads:\n"
        "  local_ams_net_id: invalid\n"
        "  local_ip: 127.0.0.1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="AMS Net ID"):
        YamlTypedConfigAdapter(tmp_path).read_system_config()
