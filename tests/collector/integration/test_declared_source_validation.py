"""Collector 必须读取服务显式指定的 site/devices，禁止默认目录回退。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from collector.infrastructure.config.validation import (
    resolve_source_paths,
    validate_collector_snapshot,
)
from core.infrastructure.config.generic_yaml import ConfigErrorCode, ConfigOperationError
from tests.collector.system.config_tree import write_config_tree

pytestmark = [pytest.mark.integration, pytest.mark.safety]


def _tree(root: Path) -> Path:
    return (
        write_config_tree(
            root,
            modbus_port=1502,
            iec104_port=2404,
            nats_servers=["nats://127.0.0.1:4222"],
            site_ref="site-A",
            devices_ref="devices-A",
        )
        / "service_collector-realtime_v1.0.0.yaml"
    )


def test_nondefault_source_files_are_used_by_full_validation(tmp_path: Path) -> None:
    service = _tree(tmp_path)
    paths = resolve_source_paths(service)
    assert paths.site == tmp_path / "instances/site_site-A_v1.0.0.yaml"
    assert paths.devices == tmp_path / "instances/device_devices-A_v1.0.0.yaml"
    validate_collector_snapshot(paths)


@pytest.mark.parametrize(
    "document",
    ["site_site-A_v1.0.0.yaml", "device_devices-A_v1.0.0.yaml"],
)
def test_no_fallback_to_valid_default_when_declared_file_invalid(
    tmp_path: Path, document: str
) -> None:
    service = _tree(tmp_path)
    instances = tmp_path / "instances"
    selected = instances / document
    alternative = yaml.safe_load(selected.read_text(encoding="utf-8"))
    alternative["file_version"] = "1.0.1"
    selected.with_name(document.replace("_v1.0.0", "_v1.0.1")).write_text(
        yaml.safe_dump(alternative), encoding="utf-8"
    )
    selected.write_text("broken: [\n", encoding="utf-8")
    with pytest.raises(ConfigOperationError) as exc:
        validate_collector_snapshot(resolve_source_paths(service))
    assert exc.value.code is ConfigErrorCode.PARSE_ERROR
    assert exc.value.path == selected
