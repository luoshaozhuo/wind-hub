"""Commander 配置入口直接消费 Core 完整领域快照。"""

from pathlib import Path

from commander.infrastructure.config.loader import load_commander_config
from core.domain import DeviceId
from core.infrastructure.config import YamlConfigAdapter
from tests.support.new_commander import write_minimal_config_tree


def test_commander_loads_domain_snapshot(tmp_path: Path) -> None:
    base = write_minimal_config_tree(tmp_path / "cfg")
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
    snapshot = YamlConfigAdapter(write_minimal_config_tree(tmp_path / "cfg")).load()

    class Source:
        def load(self):
            return snapshot

        def save(self, value):
            raise AssertionError("read-only loading must not write")

    config = load_commander_config(tmp_path, source=Source())
    assert set(config.devices).issubset(snapshot.site.devices)
    for device_id in config.devices:
        assert config.point_table_for_device(DeviceId(device_id))
