"""S2-12：热重载三种代际下 source_manifest 与 revision 的来源诚信。

- 全部生效：沿用候选清单（经过完整 SHA-256 闭包校验的单一来源）；
- 全部阻断：沿用旧清单与旧 revision，不伪装成新版本；
- 部分生效：混合代际不存在可验证的单一磁盘清单，manifest 必须为空元组，
  revision 是显式合成值，绝不能伪造候选或旧代的单一来源清单。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from shutil import copytree

import pytest

from commander.application.config_manager import CommanderConfigManager
from commander.infrastructure.service_settings import load_service_settings

pytestmark = [pytest.mark.component, pytest.mark.reload]

_REPO_CONFIGS = Path(__file__).resolve().parents[4] / "configs"

_SETPOINT_TABLE = """\
schema_version: 1
template_id: modbus_wtg
file_version: 1.0.0
protocol: modbus
points:
  setpoint_power:
    business_point_id: setpoint_power
    point_groups: [all]
    address:
      register_type: holding
      address: 200
    data_type: float32
    unit: kilowatt
"""

_BUSINESS_POINTS = """\
schema_version: 1
file_version: 1.0.0
business_points:
  setpoint_power:
    data_type: float32
    unit: kilowatt
    description: Writable active power setpoint
"""

_SERVICE_YAML = """\
schema_version: 1
file_version: 1.0.0
config_sources:
  instances:
    devices: config/instances/devices.yaml
  templates:
    root: config/templates
dispatcher:
  max_concurrency: 100
  max_pending_per_device: 10
  max_pending_global: 1000
  max_batch_size: 10
repository:
  sqlite_path: {sqlite_path}
audit:
  nats_servers: ["nats://127.0.0.1:1"]
  stream: S
  subject: s
"""

_MODEL_TEMPLATE = """\
schema_version: 1
template_id: modbus_wtg
file_version: 1.0.0
name: identity turbine
device_type_id: turbine
protocol:
  id: modbus_tcp
  version: 1.1.0
point_table:
  id: modbus_wtg
  version: 1.0.0
task:
  id: turbine_all
  version: 1.0.0
protocol_options:
  slave_id: 1
endpoint_defaults:
  port: 5020
"""


def _device(device_id: str, port: int) -> str:
    return (
        f"  {device_id}:\n"
        "    model:\n      id: modbus_wtg\n      version: 1.0.0\n"
        "    endpoint:\n      host: 127.0.0.1\n"
        f"      port: {port}\n"
        "    overrides: {}\n"
    )


def _devices_document(port_a: int, port_b: int) -> str:
    return (
        "schema_version: 1\nfile_version: 1.0.0\ndevices:\n"
        + _device("WT-A", port_a)
        + _device("WT-B", port_b)
    )


def _build_tree(tmp_root: Path, port_a: int, port_b: int) -> tuple[Path, Path]:
    root = tmp_root / "config"
    copytree(_REPO_CONFIGS / "instances", root / "instances")
    copytree(_REPO_CONFIGS / "templates", root / "templates")
    (root / "instances" / "devices.yaml").write_text(
        _devices_document(port_a, port_b), encoding="utf-8"
    )
    (root / "templates/device_models/device_model_modbus_wtg_v1.0.0.yaml").write_text(
        _MODEL_TEMPLATE, encoding="utf-8"
    )
    (root / "templates/point_tables/point_table_modbus_wtg_v1.0.0.yaml").write_text(
        _SETPOINT_TABLE, encoding="utf-8"
    )
    (root / "templates/business_points_standard.yaml").write_text(
        _BUSINESS_POINTS, encoding="utf-8"
    )
    service_yaml = tmp_root / "commander.yaml"
    service_yaml.write_text(
        _SERVICE_YAML.format(sqlite_path=str(tmp_root / "state" / "cmd.db")),
        encoding="utf-8",
    )
    return root, service_yaml


def _manager(
    service_yaml: Path,
    quiesce: Callable[[frozenset[str]], Awaitable[frozenset[str]]],
) -> CommanderConfigManager:
    manager = CommanderConfigManager(
        quiesce=quiesce,
        settings_loader=lambda: load_service_settings(service_yaml),
    )
    manager.start()
    return manager


async def _allow_all(_devices: frozenset[str]) -> frozenset[str]:
    return frozenset()


async def test_full_reload_preserves_verified_candidate_manifest(tmp_path: Path) -> None:
    root, service_yaml = _build_tree(tmp_path, 5021, 5022)
    manager = _manager(service_yaml, _allow_all)
    initial = manager.documents
    assert initial.source_manifest  # 启动即持有非空、可验证的单一来源清单

    (root / "instances" / "devices.yaml").write_text(
        _devices_document(5031, 5032), encoding="utf-8"
    )
    report = await manager.reload()

    assert report.blocked == ()
    assert manager.documents.source_manifest  # 仍为非空验证清单
    assert manager.documents.source_manifest != initial.source_manifest
    assert manager.revision != initial.revision
    assert len(manager.revision) == 64


async def test_fully_blocked_reload_keeps_old_manifest_and_revision(tmp_path: Path) -> None:
    root, service_yaml = _build_tree(tmp_path, 5021, 5022)

    async def block_all(devices: frozenset[str]) -> frozenset[str]:
        return frozenset(devices)

    manager = _manager(service_yaml, block_all)
    initial = manager.documents

    (root / "instances" / "devices.yaml").write_text(
        _devices_document(5041, 5042), encoding="utf-8"
    )
    report = await manager.reload()

    assert set(report.blocked) == {"WT-A", "WT-B"}
    assert manager.documents.source_manifest == initial.source_manifest
    assert manager.revision == initial.revision
    assert set(manager.blocked_devices()) == {"WT-A", "WT-B"}


async def test_partial_reload_emits_empty_manifest_without_fabricating_source(
    tmp_path: Path,
) -> None:
    root, service_yaml = _build_tree(tmp_path, 5021, 5022)

    async def block_b_only(devices: frozenset[str]) -> frozenset[str]:
        return frozenset({"WT-B"}) & devices

    manager = _manager(service_yaml, block_b_only)
    initial = manager.documents
    initial_fp_a = manager.device_revision("WT-A")
    initial_fp_b = manager.device_revision("WT-B")

    (root / "instances" / "devices.yaml").write_text(
        _devices_document(5051, 5052), encoding="utf-8"
    )
    report = await manager.reload()

    # WT-A 热生效、WT-B 阻断保留旧绑定：混合代际。
    assert report.changed == ("WT-A",)
    assert report.blocked == ("WT-B",)
    assert manager.device_revision("WT-A") != initial_fp_a
    assert manager.device_revision("WT-B") == initial_fp_b
    # 混合代际没有单一磁盘清单：必须显式为空，禁止伪造候选/旧清单。
    assert manager.documents.source_manifest == ()
    # 合成 revision 与两个纯代际都不同，但仍是确定性 64 位十六进制。
    assert manager.revision != initial.revision
    assert len(manager.revision) == 64
    int(manager.revision, 16)
    # 阻断设备继续留在阻断集合，直至下一次成功完整重载。
    assert "WT-B" in manager.blocked_devices()
