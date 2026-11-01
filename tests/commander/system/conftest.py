"""系统测试夹具：自管 JetStream 容器 + 真实共享配置树 + commander.yaml。"""

from __future__ import annotations

import contextlib
import re
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from shutil import copytree

import pytest

from tests.support.process import free_port

_REPO_CONFIGS = Path(__file__).resolve().parents[3] / "configs"

_COMMANDER_YAML = """\
schema_version: 1
file_version: 1
dispatcher:
  max_concurrency: 100
  max_pending_per_device: 10
  max_pending_global: 1000
  max_batch_size: 10
timeouts:
  queue_default_ms: 1000
  connect_ms: 3000
  write_default_ms: 3000
repository:
  sqlite_path: {sqlite_path}
  max_storage_bytes: 1073741824
  reserved_storage_bytes: 67108864
  retention_seconds: 1800
  cleanup_interval_seconds: 3600
  storage_warning_ratio: 0.8
  admission_stop_ratio: 0.9
audit:
  nats_servers: [{nats_url}]
  stream: {stream}
  subject: {subject}
  retry_base_ms: 100
  retry_max_ms: 5000
shutdown:
  grace_seconds: 10
"""

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


@pytest.fixture(scope="module")
def jetstream_url() -> Iterator[str]:
    """模块级独立 JetStream 容器（与既有 soak 4222 容器完全隔离）。"""
    try:
        subprocess.run(["docker", "version"], capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"SKIPPED: docker unavailable for real JetStream system test: {exc}")
    port = free_port()
    name = f"cmd-sys-js-{uuid.uuid4().hex[:10]}"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{port}:4222",
                "nats:latest",
                "-js",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except subprocess.SubprocessError as exc:
        pytest.skip(f"SKIPPED: cannot start JetStream container: {exc}")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with (
            contextlib.suppress(OSError),
            socket.create_connection(("127.0.0.1", port), timeout=0.5),
        ):
            break
        time.sleep(0.2)
    else:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.skip("SKIPPED: JetStream container did not open client port")
    time.sleep(1.0)
    try:
        yield f"nats://127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)


@pytest.fixture
def config_tree(tmp_path: Path, jetstream_url: str) -> tuple[Path, Path]:
    root = tmp_path / "config"
    copytree(_REPO_CONFIGS / "instances", root / "instances")
    copytree(_REPO_CONFIGS / "templates", root / "templates")
    return root, tmp_path / "state" / "commander.db"


def write_service_config(
    root: Path,
    db_path: Path,
    nats_url: str,
    *,
    modbus_port: int,
    stream: str,
    subject: str,
) -> Path:
    """实例化 commander.yaml，并把 WT01 端点/可写点表指向真实对端。"""
    devices = root / "instances" / "devices.yaml"
    devices_text = devices.read_text(encoding="utf-8")
    devices_text = re.sub(r"port:\s*\d+", f"port: {modbus_port}", devices_text)
    # word_order 必须走实例 overrides：create_session 只取设备实例级
    # protocol_options（设备模型模板选项不会合并——验收 B 类缺陷）。
    if "word_order: big_endian" not in devices_text:
        devices_text = devices_text.replace(
            "    overrides:\n      task:\n        desired_state: STOP\n",
            "    overrides:\n      task:\n        desired_state: STOP\n"
            "      protocol_options:\n        word_order: big_endian\n",
        )
    devices.write_text(devices_text, encoding="utf-8")
    table = root / "templates" / "point_tables" / "modbus_wtg" / "1.0.0.yaml"
    table.write_text(_SETPOINT_TABLE, encoding="utf-8")
    business = root / "templates" / "business_points.yaml"
    business.write_text(_BUSINESS_POINTS, encoding="utf-8")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    service_yaml = root.parent / "commander.yaml"
    service_yaml.write_text(
        _COMMANDER_YAML.format(
            sqlite_path=str(db_path),
            nats_url=nats_url,
            stream=stream,
            subject=subject,
        ),
        encoding="utf-8",
    )
    return service_yaml
