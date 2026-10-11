"""Collector 使用 Core 完整配置快照加载设备、Task 与 Sink。"""

from pathlib import Path

from collector.infrastructure.config.loader import load_collector_config
from core.infrastructure.config.adapter import YamlConfigAdapter

ROOT = Path(__file__).resolve().parents[4]


def test_collector_loads_domain_snapshot_without_units_yaml() -> None:
    base = ROOT / "configs" / "example_modbus"
    assert not (base / "units.yaml").exists()
    snapshot = YamlConfigAdapter(base).load()
    config = load_collector_config(base)

    assert config.tasks == snapshot.tasks
    assert config.device_models == snapshot.device_models
    assert config.point_tables == snapshot.point_tables
    assert set(config.sinks) == set(snapshot.sinks)
    assert config.devices == {
        device_id: device
        for device_id, device in snapshot.site.devices.items()
        if device.enabled
    }


def test_collector_accepts_snapshot_port_source(tmp_path: Path) -> None:
    snapshot = YamlConfigAdapter(ROOT / "configs" / "example_ads").load()

    class Source:
        def load(self):
            return snapshot

        def save(self, value):
            raise AssertionError("loading must never write")

    config = load_collector_config(tmp_path, source=Source())
    assert config.tasks == snapshot.tasks
    assert config.ads_subscribe_devices == snapshot.ads_subscribe_devices
