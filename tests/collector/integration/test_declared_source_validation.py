"""Collector 必须读取服务显式指定的 site/devices，禁止默认目录回退。"""

from __future__ import annotations

from pathlib import Path

import pytest

from collector.infrastructure.config.validation import (
    resolve_source_paths,
    validate_collector_snapshot,
)
from core.application.errors import ConfigError
from tests.collector.system.config_tree import write_config_tree

pytestmark = [pytest.mark.integration, pytest.mark.safety]


def _tree(root: Path) -> Path:
    return write_config_tree(
        root,
        modbus_port=1502,
        iec104_port=2404,
        nats_servers=["nats://127.0.0.1:4222"],
        site_ref="custom/site-A.yaml",
        devices_ref="custom/devices-A.yaml",
    ) / "collector.yaml"


def test_nondefault_source_files_are_used_by_full_validation(tmp_path: Path) -> None:
    service = _tree(tmp_path)
    paths = resolve_source_paths(service)
    assert paths.site == tmp_path / "custom/site-A.yaml"
    assert paths.devices == tmp_path / "custom/devices-A.yaml"
    validate_collector_snapshot(paths)


@pytest.mark.parametrize("document", ["site-A.yaml", "devices-A.yaml"])
def test_no_fallback_to_valid_default_when_declared_file_invalid(
    tmp_path: Path, document: str
) -> None:
    service = _tree(tmp_path)
    custom = tmp_path / "custom"
    default = tmp_path / "instances"
    default.mkdir()
    (default / "site.yaml").write_bytes((custom / "site-A.yaml").read_bytes())
    (default / "devices.yaml").write_bytes((custom / "devices-A.yaml").read_bytes())
    (custom / document).write_text("broken: [\n", encoding="utf-8")
    paths = resolve_source_paths(service)
    with pytest.raises(ConfigError):
        validate_collector_snapshot(paths)
