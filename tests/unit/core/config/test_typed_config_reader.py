"""Core 类型化配置读取契约测试。"""

from __future__ import annotations

import pytest

from core.application import ConfigError
from core.application.port import ConfigPort, ConfigTopic
from core.infrastructure.config import YamlConfigAdapter


def test_adapter_returns_independent_validated_topics(tmp_path):
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

    adapter = YamlConfigAdapter(tmp_path)
    assert isinstance(adapter, ConfigPort)
    assert adapter.read(ConfigTopic.DEVICES).devices == {}
    assert adapter.read(ConfigTopic.DEVICE_MODELS).device_models == {}
    assert adapter.read(ConfigTopic.POINTS).tables == {}
    assert adapter.read(ConfigTopic.TASKS).tasks == {}
    assert adapter.read(ConfigTopic.UNITS).units == {}
    assert adapter.read(ConfigTopic.SINKS).sinks == []
    assert adapter.read(ConfigTopic.SYSTEM).runtime.connect_timeout == 10.0


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
        YamlConfigAdapter(tmp_path).read(ConfigTopic.TASKS)


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
    points = YamlConfigAdapter(tmp_path).read(ConfigTopic.POINTS)
    assert "power" in points.tables["child"].points


def test_invalid_sink_is_rejected_without_other_topics(tmp_path):
    (tmp_path / "sinks.yaml").write_text(
        "sinks:\n  - name: bad\n    type: invalid\n    connection: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        YamlConfigAdapter(tmp_path).read(ConfigTopic.SINKS)


def test_system_config_is_typed_and_immutable(tmp_path):
    (tmp_path / "system.yaml").write_text(
        "site: {site_id: s1}\n"
        "runtime: {queue_maxsize: 500, read_timeout: 2.5}\n"
        "ads: {local_ams_net_id: 1.2.3.4.5.6, local_ip: 127.0.0.1}\n",
        encoding="utf-8",
    )
    config = YamlConfigAdapter(tmp_path).read(ConfigTopic.SYSTEM)
    assert config.site_id == "s1"
    assert config.runtime.queue_maxsize == 500
    assert config.runtime.read_timeout == 2.5
    assert config.runtime.connect_timeout is None
    assert config.ads is not None
    assert config.ads.username == "Administrator"
    assert config.ads.password == ""
    with pytest.raises(AttributeError):
        config.runtime.queue_maxsize = 1


def test_system_config_defaults_are_absent(tmp_path):
    (tmp_path / "system.yaml").write_text("site: {site_id: s1}\n", encoding="utf-8")
    config = YamlConfigAdapter(tmp_path).read(ConfigTopic.SYSTEM)
    assert config.ads is None
    assert config.runtime.queue_maxsize is None
    assert config.runtime.backpressure_policy is None
    assert config.runtime.read_timeout is None


@pytest.mark.parametrize(
    "runtime_yaml, message",
    [
        ("{nested: {values: [1]}}", "unknown keys"),
        ("{queue_maxsize: 0}", "queue_maxsize"),
        ("{backpressure_policy: drop_random}", "backpressure_policy"),
        ("{connect_timeout: -1}", "connect_timeout"),
    ],
)
def test_system_config_rejects_invalid_runtime(tmp_path, runtime_yaml, message):
    (tmp_path / "system.yaml").write_text(
        f"runtime: {runtime_yaml}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=message):
        YamlConfigAdapter(tmp_path).read(ConfigTopic.SYSTEM)


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
    config = YamlConfigAdapter(tmp_path).read(ConfigTopic.POINTS)
    with pytest.raises(TypeError):
        config.tables["main"].points["speed"] = None


def test_device_models_and_instances_are_independently_readable(tmp_path):
    (tmp_path / "device_models.yaml").write_text(
        "device_types: {}\ndevice_models: {}\n",
        encoding="utf-8",
    )
    adapter = YamlConfigAdapter(tmp_path)
    assert adapter.read(ConfigTopic.DEVICE_MODELS).device_models == {}
    with pytest.raises(ConfigError, match="not found"):
        adapter.read(ConfigTopic.DEVICES)


def test_task_config_returns_immutable_definitions(tmp_path):
    (tmp_path / "tasks.yaml").write_text(
        "tasks:\n"
        "  - task_id: sample\n"
        "    device: wt01\n"
        "    point_group: fast\n"
        "    targets: [{sink: archive}]\n",
        encoding="utf-8",
    )
    tasks = YamlConfigAdapter(tmp_path).read(ConfigTopic.TASKS).tasks
    assert tasks["sample"].targets == ("archive",)
    with pytest.raises(AttributeError):
        tasks["sample"].task_id = "changed"


def test_units_are_independent_and_immutable(tmp_path):
    (tmp_path / "units.yaml").write_text(
        "units:\n  none:\n    symbol: ''\n    name: Dimensionless\n",
        encoding="utf-8",
    )
    result = YamlConfigAdapter(tmp_path).read(ConfigTopic.UNITS)
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
    adapter = YamlConfigAdapter(tmp_path)
    models = adapter.read(ConfigTopic.DEVICE_MODELS)
    devices = adapter.read(ConfigTopic.DEVICES)
    points = adapter.read(ConfigTopic.POINTS)
    with pytest.raises(TypeError):
        models.device_models["m1"].connection_defaults["port"] = 852
    with pytest.raises(TypeError):
        devices.devices["d1"].endpoint.extensions["nested"][0] = 3
    with pytest.raises(AttributeError):
        points.tables["main"].points["p1"].scale = 10.0


@pytest.mark.parametrize("section", ["ads", "runtime"])
def test_system_config_rejects_non_mapping_sections(tmp_path, section):
    (tmp_path / "system.yaml").write_text(
        f"{section}: invalid\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must be a mapping"):
        YamlConfigAdapter(tmp_path).read(ConfigTopic.SYSTEM)


def test_system_config_validates_ads_identity(tmp_path):
    (tmp_path / "system.yaml").write_text(
        "ads:\n"
        "  local_ams_net_id: invalid\n"
        "  local_ip: 127.0.0.1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="AMS Net ID"):
        YamlConfigAdapter(tmp_path).read(ConfigTopic.SYSTEM)
