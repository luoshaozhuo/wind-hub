"""配置文件写入失败时的回滚行为。"""

from collections.abc import Mapping
from pathlib import Path
from shutil import copy2
from typing import Any

import pytest

from core.application.errors import ConfigError
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


def test_save_rolls_back_during_commit_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """暂存往返验证通过后，正式写入阶段失败也必须逐文件回滚。"""
    source = ROOT / "tests" / "fixtures" / "configs" / "example_modbus"
    for filename in FILES:
        copy2(source / filename, tmp_path / filename)
    original_bytes = {filename: (tmp_path / filename).read_bytes() for filename in FILES}
    adapter = YamlConfigAdapter(tmp_path)
    snapshot = adapter.load()
    original_writer = adapter_module.write_yaml_mapping_atomic  # type: ignore[attr-defined]

    def fail_only_in_commit_phase(path: Path, data: Mapping[str, Any]) -> None:
        # 暂存目录是 base 下的临时子目录；提交阶段直接写 base 根。
        if Path(path).parent == tmp_path and Path(path).name == "devices.yaml":
            raise OSError("injected commit failure")
        original_writer(path, data)

    monkeypatch.setattr(adapter_module, "write_yaml_mapping_atomic", fail_only_in_commit_phase)
    with pytest.raises(OSError, match="injected commit"):
        adapter.save(snapshot)

    for filename in FILES:
        assert (tmp_path / filename).read_bytes() == original_bytes[filename]


def test_save_rejected_when_roundtrip_is_lossy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """暂存往返后快照不一致 → ConfigError，且 base 文件一个字节都不改。"""
    source = ROOT / "tests" / "fixtures" / "configs" / "example_modbus"
    for filename in FILES:
        copy2(source / filename, tmp_path / filename)
    original_bytes = {filename: (tmp_path / filename).read_bytes() for filename in FILES}
    adapter = YamlConfigAdapter(tmp_path)
    snapshot = adapter.load()

    class _AlwaysDifferent:
        has_changes = True

    monkeypatch.setattr(
        adapter_module, "diff_config_snapshots", lambda previous, current: _AlwaysDifferent()
    )
    with pytest.raises(ConfigError, match="losslessly"):
        adapter.save(snapshot)

    for filename in FILES:
        assert (tmp_path / filename).read_bytes() == original_bytes[filename]
