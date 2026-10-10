"""YamlConfigAdapter.save 语义测试。

- 各主题 read→save→read 语义等价；
- 点表保存为继承展开后的完整形式（无 extends/remove_points）；
- 原子写入：失败不留临时文件、不破坏原文件、不影响其他主题文件。
"""

from __future__ import annotations

import pytest

from core.application import ConfigError
from core.application.port import ConfigTopic
from core.application.sink_config import SinksConfig
from core.domain.config import (
    DeviceInstanceConfig,
    DeviceModelConfig,
    DeviceModelsConfig,
    DevicesConfig,
    EndpointConfig,
    PointConfig,
    PointTableConfig,
    PointTablesConfig,
    RuntimeSettings,
    SystemConfig,
    TaskConfig,
    TasksConfig,
    UnitDefinitionConfig,
    UnitsConfig,
)
from core.infrastructure.config import YamlConfigAdapter

ALL_TOPICS = tuple(ConfigTopic)


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def site(tmp_path):
    _write(tmp_path / "system.yaml", "site: {site_id: s1}\n")
    _write(
        tmp_path / "device_models.yaml",
        "device_types: {turbine: {name: 风机}}\n"
        "device_models:\n"
        "  mod: {device_type: turbine, protocol: modbus, point_table: tab}\n",
    )
    _write(
        tmp_path / "devices.yaml",
        "devices:\n"
        "  - device_id: dev1\n"
        "    model: mod\n"
        "    endpoint: {host: 127.0.0.1, port: 502}\n",
    )
    _write(
        tmp_path / "points.yaml",
        "point_tables:\n"
        "  base:\n"
        "    protocol: modbus\n"
        "    points:\n"
        "      - point_id: p1\n"
        "        point_groups: [g]\n"
        "        address: {register_type: holding, address: 100}\n"
        "        data_type: float32\n"
        "  tab:\n"
        "    extends: base\n",
    )
    _write(tmp_path / "units.yaml", "units: {none: {symbol: ''}}\n")
    _write(
        tmp_path / "tasks.yaml",
        "tasks:\n"
        "  - task_id: t1\n"
        "    device: dev1\n"
        "    point_group: g\n"
        "    interval: 1.0\n"
        "    targets: [{sink: s1}]\n",
    )
    _write(
        tmp_path / "sinks.yaml",
        "sinks:\n"
        "  - name: s1\n"
        "    type: file\n"
        "    connection: {path: /tmp/out.csv}\n"
        "    points:\n"
        "      - source: {device_id: dev1, point_id: p1}\n"
        "        address: {field: value}\n",
    )
    return tmp_path


def test_read_save_read_roundtrip_all_topics(site):
    service = YamlConfigAdapter(site)
    for topic in ALL_TOPICS:
        original = service.read(topic)
        service.save(topic, original)
        reread = service.read(topic)
        assert reread == original, f"{topic}: read→save→read 语义不等价"


def test_save_points_expands_inheritance(site):
    """保存后的 points.yaml 不含 extends/remove_points，点位为完整展开形式。"""
    service = YamlConfigAdapter(site)
    points = service.read(ConfigTopic.POINTS)
    service.save(ConfigTopic.POINTS, points)

    text = (site / "points.yaml").read_text(encoding="utf-8")
    assert "extends" not in text
    assert "remove_points" not in text
    # 继承来的表 tab 带完整点位
    saved = service.read(ConfigTopic.POINTS)
    assert saved.tables["tab"].points["p1"].address["address"] == 100


def test_save_rejects_topic_type_mismatch(site):
    service = YamlConfigAdapter(site)
    units = service.read(ConfigTopic.UNITS)
    with pytest.raises(ConfigError, match="expects config of type"):
        service.save(ConfigTopic.DEVICES, units)
    adapter = YamlConfigAdapter(site)
    with pytest.raises(ConfigError, match="expects config of type"):
        adapter.save(ConfigTopic.TASKS, units)


def test_save_validates_before_writing(site):
    """非法 VO（键与业务 ID 不一致）在写盘前被拒绝。"""
    before = (site / "devices.yaml").read_bytes()
    with pytest.raises(ValueError, match="does not match device_id"):
        DevicesConfig(
            devices={
                "other": DeviceInstanceConfig(
                    device_id="d",
                    model="mod",
                    device_group=None,
                    endpoint=EndpointConfig(host="h", port=1, extensions={}),
                    enabled=True,
                )
            }
        )
    assert (site / "devices.yaml").read_bytes() == before


def test_failed_save_leaves_no_temp_files_and_preserves_original(site, monkeypatch):
    service = YamlConfigAdapter(site)
    original_bytes = (site / "tasks.yaml").read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        service.save(
            ConfigTopic.TASKS,
            TasksConfig(
                tasks={
                    "t2": TaskConfig(
                        task_id="t2", point_group="g", targets=("s1",), device="dev1"
                    )
                }
            ),
        )
    assert (site / "tasks.yaml").read_bytes() == original_bytes
    leftovers = [p.name for p in site.iterdir() if p.name not in _expected_files()]
    assert leftovers == []


def test_save_does_not_touch_other_topic_files(site):
    service = YamlConfigAdapter(site)
    before = {p.name: p.read_bytes() for p in site.glob("*.yaml")}
    service.save(ConfigTopic.UNITS, service.read(ConfigTopic.UNITS))
    for name, content in before.items():
        if name == "units.yaml":
            continue
        assert (site / name).read_bytes() == content, f"{name} 被意外修改"


def _expected_files() -> set[str]:
    return {
        "system.yaml",
        "device_models.yaml",
        "devices.yaml",
        "points.yaml",
        "units.yaml",
        "tasks.yaml",
        "sinks.yaml",
    }


def test_save_constructed_vos_roundtrip(tmp_path):
    """直接构造 VO 保存后可读回等价对象（不依赖既有文件内容）。"""
    site = tmp_path
    _write(site / "system.yaml", "site: {site_id: s1}\n")
    _write(site / "device_models.yaml", "device_models: {}\n")
    _write(site / "devices.yaml", "devices: []\n")
    _write(site / "points.yaml", "point_tables: {}\n")
    _write(site / "units.yaml", "units: {}\n")
    _write(site / "tasks.yaml", "tasks: []\n")
    _write(site / "sinks.yaml", "sinks: []\n")
    service = YamlConfigAdapter(site)

    system = SystemConfig(
        site_id="s2",
        site_name="现场",
        runtime=RuntimeSettings(queue_maxsize=64),
    )
    service.save(ConfigTopic.SYSTEM, system)
    assert service.read(ConfigTopic.SYSTEM) == system

    models = DeviceModelsConfig(
        device_types={"turbine": None},
        device_models={
            "mod": DeviceModelConfig(
                device_type="turbine",
                manufacturer="m",
                model=None,
                protocol="modbus",
                point_table="tab",
                read_mode=None,
                properties={"k": "v"},
                connection_defaults={"port": 502},
            )
        },
    )
    service.save(ConfigTopic.DEVICE_MODELS, models)
    assert service.read(ConfigTopic.DEVICE_MODELS) == models

    points = PointTablesConfig(
        tables={
            "tab": PointTableConfig(
                protocol="modbus",
                points={
                    "p1": PointConfig(
                        point_id="p1",
                        variable_name="P1",
                        point_groups=("g",),
                        address={"register_type": "holding", "address": 100},
                        data_type="float32",
                        scale=0.5,
                        offset=1.0,
                        unit="none",
                        description="desc",
                    )
                },
            )
        }
    )
    service.save(ConfigTopic.POINTS, points)
    assert service.read(ConfigTopic.POINTS) == points

    units = UnitsConfig(units={"m/s": UnitDefinitionConfig(symbol="m/s", name="风速")})
    service.save(ConfigTopic.UNITS, units)
    assert service.read(ConfigTopic.UNITS) == units

    tasks = TasksConfig(
        tasks={"t": TaskConfig(task_id="t", point_group="g", targets=("s1",), device_group="grp")}
    )
    service.save(ConfigTopic.TASKS, tasks)
    assert service.read(ConfigTopic.TASKS) == tasks

    sinks = service.read(ConfigTopic.SINKS)
    assert isinstance(sinks, SinksConfig)
