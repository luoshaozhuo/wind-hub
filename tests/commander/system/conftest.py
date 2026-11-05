"""系统测试夹具：自管 JetStream 容器 + 真实共享配置树 + commander.yaml。"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from shutil import copytree

import pytest

from tests.commander.grpc_harness import (
    GrpcStack,
    Pki,
    build_pki,
    build_stack,
)
from tests.support.process import free_port

_REPO_CONFIGS = Path(__file__).resolve().parents[3] / "configs"

_COMMANDER_YAML = """\
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

_MODELS_TABLE = """\
schema_version: 1
template_id: modbus_wtg
file_version: 1.0.0
name: Commander acceptance Modbus turbine
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
    """实例化 commander.yaml，并把 WT01 端点/可写点表指向真实对端。

    共享配置树包含 GW/TZ 系列真实设备；e2e 额外注入一台仅用于验收的
    WT01（modbus_wtg 1.0.0），指向本用例自管的真实 Modbus 对端。
    """
    devices = root / "instances" / "devices.yaml"
    devices_text = devices.read_text(encoding="utf-8")
    # 重启用例会在同一配置树上二次调用本函数，注入必须幂等。
    if "\n  WT01:" not in devices_text:
        # word_order 必须走实例 overrides：create_session 只取设备实例级
        # protocol_options（设备模型模板选项不会合并——验收 B 类缺陷）。
        devices_text += f"""
  WT01:
    name: "Commander acceptance Modbus WT01"
    enabled: true
    model:
      id: modbus_wtg
      version: 1.0.0
    endpoint:
      host: 127.0.0.1
      port: {modbus_port}
    overrides:
      task:
        desired_state: STOP
      protocol_options:
        word_order: big_endian
"""
    devices.write_text(devices_text, encoding="utf-8")
    table = root / "templates" / "point_tables" / "modbus_wtg_1.0.0.yaml"
    table.write_text(_SETPOINT_TABLE, encoding="utf-8")
    model = root / "templates" / "device_models" / "modbus_wtg_1.0.0.yaml"
    model.write_text(_MODELS_TABLE, encoding="utf-8")
    # 保留真实业务点目录（其余 GW/TZ 设备的有效配置解析依赖它），
    # 仅补入验收点表引用的 setpoint_power。
    business = root / "templates" / "business_points.yaml"
    business_text = business.read_text(encoding="utf-8")
    if "  setpoint_power:" not in business_text:
        business_text = business_text.replace(
            "business_points:\n",
            "business_points:\n" + _BUSINESS_POINTS.split("business_points:\n", 1)[1],
            1,
        )
    business.write_text(business_text, encoding="utf-8")
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


# ---------------------------------------------------------------------------
# §14.4 System Test 共享真实链路夹具：mTLS + build_commander + 安全端口
# （构造逻辑见 tests/commander/grpc_harness.py，与 integration 层同一份）
# ---------------------------------------------------------------------------


@pytest.fixture
def sys_pki(tmp_path: Path) -> Pki:
    return build_pki(tmp_path)


@pytest.fixture
async def grpc_stack(tmp_path: Path, sys_pki: Pki) -> GrpcStack:
    stack, server, service = await build_stack(
        tmp_path, sys_pki, label="system", stream_prefix="CMD_SYS"
    )
    try:
        yield stack
    finally:
        await server.stop(grace=5)
        await service.stop()


@pytest.fixture
async def grpc_stack_prepared(tmp_path: Path, sys_pki: Pki, request) -> GrpcStack:
    """带预启动配置钩子的系统栈（request.param 为 prepare 回调）。"""
    stack, server, service = await build_stack(
        tmp_path,
        sys_pki,
        label="system",
        stream_prefix="CMD_SYS",
        prepare=request.param,
    )
    try:
        yield stack
    finally:
        await server.stop(grace=5)
        await service.stop()


@pytest.fixture
async def grpc_stack_modbus(tmp_path: Path, sys_pki: Pki, modbus_server) -> GrpcStack:
    """WT01 指向真实 Modbus TCP 从站（pymodbus，非替身协议实现）。"""
    stack, server, service = await build_stack(
        tmp_path,
        sys_pki,
        modbus_port=modbus_server.port,
        label="system",
        stream_prefix="CMD_SYS",
    )
    try:
        yield stack
    finally:
        await server.stop(grace=5)
        await service.stop()


@pytest.fixture
async def grpc_stack_jetstream(
    tmp_path: Path, sys_pki: Pki, modbus_server, jetstream_url: str
) -> tuple[GrpcStack, str]:
    """真实 Modbus 从站 + 真实 JetStream 容器的完整系统栈。"""
    stack, server, service = await build_stack(
        tmp_path,
        sys_pki,
        modbus_port=modbus_server.port,
        nats_url=jetstream_url,
        label="system",
        stream_prefix="CMD_SYS",
    )
    try:
        yield stack, jetstream_url
    finally:
        await server.stop(grace=5)
        await service.stop()
