"""多设备分组的 YAML 配置往返测试。"""

from dataclasses import replace
from pathlib import Path
from shutil import copy2

from core.application.config_diff import diff_config_snapshots
from core.infrastructure.config.adapter import YamlConfigAdapter

ROOT = Path(__file__).resolve().parents[4]


def test_device_multi_groups_survive_save_and_reload(tmp_path: Path) -> None:
    source = ROOT / "configs" / "example_modbus"
    for filename in (
        "system.yaml", "device_models.yaml", "devices.yaml", "points.yaml",
        "business_points.yaml", "tasks.yaml", "sinks.yaml",
    ):
        copy2(source / filename, tmp_path / filename)
    adapter = YamlConfigAdapter(tmp_path)
    original = adapter.load()
    device_id, device = next(iter(original.site.devices.items()))
    group = device.device_group_ids[0]
    extra = "new-group"
    updated_device = replace(device, device_group_ids=(group, extra))
    site = replace(original.site, devices={
        **original.site.devices, device_id: updated_device,
    })
    modified = replace(
        original,
        site=site,
        device_groups={
            **original.device_groups,
            extra: replace(original.device_groups[group], device_group_id=extra, name=extra),
        },
    )
    adapter.save(modified)
    reloaded = adapter.load()
    assert not diff_config_snapshots(modified, reloaded).sections["devices"].has_changes
    assert reloaded.site.devices[device_id].device_group_ids == (group, extra)
