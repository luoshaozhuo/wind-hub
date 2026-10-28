"""配置文件写入失败时的回滚行为。"""

from collections.abc import Mapping
from pathlib import Path
from shutil import copy2
from typing import Any

import pytest

from core.infrastructure.config import adapter as adapter_module
from core.infrastructure.config.adapter import YamlConfigAdapter

ROOT = Path(__file__).resolve().parents[3]
FILES = (
    "system.yaml",
    "units.yaml",
    "device_models.yaml",
    "devices.yaml",
    "points.yaml",
    "business_points.yaml",
    "tasks.yaml",
    "sinks.yaml",
)


def test_save_rolls_back_when_one_file_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ROOT / "tests" / "fixtures" / "configs" / "example_modbus"
    for filename in FILES:
        copy2(source / filename, tmp_path / filename)
    original_bytes = {filename: (tmp_path / filename).read_bytes() for filename in FILES}
    adapter = YamlConfigAdapter(tmp_path)
    snapshot = adapter.load()
    original_writer = adapter_module.write_yaml_mapping_atomic  # type: ignore[attr-defined]  # adapter 未显式再导出该符号

    def fail_on_target(path: Path, data: Mapping[str, Any]) -> None:
        if Path(path).name == "devices.yaml":
            raise OSError("injected save failure")
        original_writer(path, data)

    monkeypatch.setattr(adapter_module, "write_yaml_mapping_atomic", fail_on_target)
    with pytest.raises(OSError, match="injected"):
        adapter.save(snapshot)
    for filename in FILES:
        assert (tmp_path / filename).read_bytes() == original_bytes[filename]
    assert not (tmp_path / ".active-config").exists()
