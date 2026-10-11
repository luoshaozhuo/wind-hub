"""Commander 配置入口直接消费 Core 完整领域快照。"""

from pathlib import Path

import pytest

from commander.infrastructure.config.loader import load_commander_config
from core.application.errors import ConfigError
from core.domain import DeviceId
from core.infrastructure.config import YamlConfigAdapter

ROOT = Path(__file__).resolve().parents[4]


def test_commander_loads_domain_snapshot_without_units_yaml() -> None:
    base = ROOT / "configs" / "example_modbus"
    assert not (base / "units.yaml").exists()
    snapshot = YamlConfigAdapter(base).load()
    commander = load_commander_config(base)
    assert commander.point_tables == snapshot.point_tables
    assert commander.device_models == snapshot.device_models
    assert commander.business_points == snapshot.business_points
    assert commander.devices == {
        key: device for key, device in snapshot.site.devices.items() if device.enabled
    }


def test_commander_configport_source_is_full_snapshot(tmp_path: Path) -> None:
    base = ROOT / "configs" / "example_modbus"
    snapshot = YamlConfigAdapter(base).load()

    class Source:
        def load(self):
            return snapshot

        def save(self, value):
            raise AssertionError("read-only loading must not write")

    config = load_commander_config(tmp_path, source=Source())
    assert set(config.devices).issubset(snapshot.site.devices)
    for device_id in config.devices:
        assert config.point_table_for_device(DeviceId(device_id))
