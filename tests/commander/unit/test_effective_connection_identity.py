"""重复连接身份必须按 effective 合成结果判定（B1 残留项）。

型号 endpoint_defaults.port / model protocol_options 与实例显式配置在
物理层等价时，原始配置文本不同也必须判为重复；反之物理目标不同不得误杀。
"""

from __future__ import annotations

from pathlib import Path
from shutil import copytree

import pytest

from commander.application.config_manager import CommanderConfigManager
from commander.infrastructure.service_settings import load_service_settings

_REPO_CONFIGS = Path(__file__).resolve().parents[3] / "configs"

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
file_version: 1
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
  version: 1.0.0
point_table:
  id: modbus_wtg
  version: 1.0.0
task:
  id: turbine_all
  version: 1.0.0
protocol_options:
  slave_id: {slave_id}
endpoint_defaults:
  port: {model_port}
"""


def _build_tree(tmp_root: Path, devices_yaml: str, *, model_port: int, slave_id: int = 1):
    root = tmp_root / "config"
    copytree(_REPO_CONFIGS / "instances", root / "instances")
    copytree(_REPO_CONFIGS / "templates", root / "templates")
    (root / "instances" / "devices.yaml").write_text(devices_yaml, encoding="utf-8")
    (root / "templates/device_models/modbus_wtg_1.0.0.yaml").write_text(
        _MODEL_TEMPLATE.format(model_port=model_port, slave_id=slave_id),
        encoding="utf-8",
    )
    (root / "templates/point_tables/modbus_wtg_1.0.0.yaml").write_text(
        _SETPOINT_TABLE, encoding="utf-8"
    )
    (root / "templates/business_points.yaml").write_text(_BUSINESS_POINTS, encoding="utf-8")
    service_yaml = tmp_root / "commander.yaml"
    service_yaml.write_text(
        _SERVICE_YAML.format(sqlite_path=str(tmp_root / "state" / "cmd.db")),
        encoding="utf-8",
    )
    return root, service_yaml


def _device(device_id: str, *, port: str, overrides: str) -> str:
    return (
        f"  {device_id}:\n"
        "    model:\n      id: modbus_wtg\n      version: 1.0.0\n"
        "    endpoint:\n      host: 127.0.0.1\n"
        f"{port}"
        f"{overrides}\n"
    )


def _manager(root: Path, service_yaml: Path) -> CommanderConfigManager:
    async def _no_quiesce(_devices: frozenset[str]) -> frozenset[str]:
        return frozenset()

    manager = CommanderConfigManager(
        root,
        quiesce=_no_quiesce,
        settings_loader=lambda: load_service_settings(service_yaml),
    )
    manager.start()
    return manager


_NO_OVERRIDES = "    overrides: {}"
_SLAVE_OVERRIDE = "    overrides:\n      protocol_options:\n        slave_id: 1"


def test_model_default_port_equals_explicit_port_is_duplicate(tmp_path) -> None:
    """A 端口缺省走型号默认 5020；B 显式写 5020：物理同址必须拒绝。"""
    devices = (
        "schema_version: 1\nfile_version: 1.0.0\ndevices:\n"
        + _device("WT-A", port="", overrides=_NO_OVERRIDES)
        + _device("WT-B", port="      port: 5020\n", overrides=_NO_OVERRIDES)
    )
    root, service_yaml = _build_tree(tmp_path, devices, model_port=5020)
    with pytest.raises(ValueError, match="duplicate protocol connection"):
        _manager(root, service_yaml)


def test_model_default_option_equals_instance_option_is_duplicate(tmp_path) -> None:
    """slave_id 一个写在型号模板、一个写在实例 overrides：合成后同址必须拒绝。"""
    devices = (
        "schema_version: 1\nfile_version: 1.0.0\ndevices:\n"
        + _device("WT-A", port="      port: 5020\n", overrides=_NO_OVERRIDES)
        + _device("WT-B", port="      port: 5020\n", overrides=_SLAVE_OVERRIDE)
    )
    root, service_yaml = _build_tree(tmp_path, devices, model_port=5020, slave_id=1)
    with pytest.raises(ValueError, match="duplicate protocol connection"):
        _manager(root, service_yaml)


def test_distinct_effective_ports_are_not_duplicate(tmp_path) -> None:
    """A 缺省走型号 5020；B 显式 5021：物理不同址必须放行。"""
    devices = (
        "schema_version: 1\nfile_version: 1.0.0\ndevices:\n"
        + _device("WT-A", port="", overrides=_NO_OVERRIDES)
        + _device("WT-B", port="      port: 5021\n", overrides=_NO_OVERRIDES)
    )
    root, service_yaml = _build_tree(tmp_path, devices, model_port=5020)
    manager = _manager(root, service_yaml)
    assert manager.device_revision("WT-A") is not None
    assert manager.device_revision("WT-B") is not None
