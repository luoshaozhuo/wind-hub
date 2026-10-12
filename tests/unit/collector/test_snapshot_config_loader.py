"""Collector 使用 Core 完整配置快照加载设备、Task 与 Sink。"""

from pathlib import Path

from collector.infrastructure.config.loader import load_collector_config
from core.infrastructure.config.adapter import YamlConfigAdapter
from tests.support.new_collector import write_collector_config_tree


def test_collector_loads_domain_snapshot(tmp_path: Path) -> None:
    base = write_collector_config_tree(tmp_path / "cfg")
    assert (base / "units.yaml").exists()
    snapshot = YamlConfigAdapter(base).load()
    config = load_collector_config(YamlConfigAdapter(base))

    assert config.tasks == snapshot.tasks
    assert config.device_models == snapshot.device_models
    assert config.point_tables == snapshot.point_tables
    assert set(config.sinks) == set(snapshot.sinks)
    assert config.devices == {
        device_id: device for device_id, device in snapshot.site.devices.items() if device.enabled
    }


def test_collector_accepts_snapshot_port_source(tmp_path: Path) -> None:
    snapshot = YamlConfigAdapter(write_collector_config_tree(tmp_path / "cfg")).load()

    class Source:
        def load(self):
            return snapshot

        def save(self, value):
            raise AssertionError("loading must never write")

    config = load_collector_config(Source())
    assert config.tasks == snapshot.tasks
    assert config.ads_subscribe_devices == snapshot.ads_subscribe_devices
