"""配置版本发布、不可变性与失败保护。"""

from dataclasses import replace
from pathlib import Path
from shutil import copy2

import pytest

from core.application.errors import ConfigError
from core.infrastructure.config.adapter import YamlConfigAdapter
from core.infrastructure.config.digest import config_dir_digest
from core.infrastructure.config.yaml import active_config_dir

ROOT = Path(__file__).resolve().parents[4]
FILES = (
    "system.yaml", "device_models.yaml", "devices.yaml", "points.yaml",
    "business_points.yaml", "tasks.yaml", "sinks.yaml",
)


def _adapter(tmp_path: Path) -> YamlConfigAdapter:
    base = ROOT / "configs" / "example_modbus"
    for filename in FILES:
        copy2(base / filename, tmp_path / filename)
    return YamlConfigAdapter(tmp_path)


def test_snapshot_nested_configuration_is_immutable(tmp_path: Path) -> None:
    snapshot = _adapter(tmp_path).load()
    device_id = next(iter(snapshot.protocol_options_by_device))
    with pytest.raises(TypeError):
        snapshot.protocol_options_by_device[device_id]["timeout"] = 99
    model = next(iter(snapshot.device_models.values()))
    with pytest.raises(TypeError):
        model.connection_defaults["port"] = 999
    sink = next(iter(snapshot.sinks.values()))
    with pytest.raises(TypeError):
        sink.connection["path"] = "other.csv"


def test_bad_generation_pointer_does_not_load_unpublished_files(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    before = adapter.load()
    (tmp_path / ".active-config").write_text("../unsafe", encoding="ascii")
    with pytest.raises(ConfigError, match="Invalid active"):
        adapter.load()
    (tmp_path / ".active-config").unlink()
    assert adapter.load() == before


def test_invalid_snapshot_never_changes_active_version(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    valid = adapter.load()
    adapter.save(valid)
    generation = active_config_dir(tmp_path)
    digest = config_dir_digest(tmp_path)
    device_id = next(iter(valid.site.devices))
    broken = replace(
        valid,
        site=replace(valid.site, devices={
            **valid.site.devices,
            device_id: replace(
                valid.site.devices[device_id],
                device_model_id="missing-model",
            ),
        }),
    )
    with pytest.raises((ConfigError, ValueError, KeyError)):
        adapter.save(broken)
    assert active_config_dir(tmp_path) == generation
    assert config_dir_digest(tmp_path) == digest
    assert adapter.load() == valid
