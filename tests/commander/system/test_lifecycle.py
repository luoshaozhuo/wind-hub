"""§14.4 表 14-2 生命周期系统测试（UR-01/02/03，UC-01/06/07）。

真实组合根 build_commander、真实磁盘 SQLite、真实配置树与 mTLS 端口。
环境说明：测试进程内重建组合根模拟独立进程的重启（句柄释放/文件锁/
重开均为真实）；OS 进程级 kill/重启变体在验收报告中记 NOT_RUN。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from shutil import copytree

import pytest
import yaml

from commander.infrastructure.assembly import build_commander
from commander.infrastructure.grpc.snapshots import ConfigSnapshots
from contracts import commander_pb2, common_pb2
from core.application.protocol_registry import ProtocolRegistry
from core.infrastructure.protocol.modbus import ModbusDriver
from tests.commander.grpc_harness import _REPO_CONFIGS, build_stack

pytestmark = [pytest.mark.system, pytest.mark.reliability, pytest.mark.real_service]

# RFC5737 TEST-NET-1：不可路由地址，TCP SYN 无响应，用于制造真实连接悬挂。
_BLACKHOLE_HOST = "192.0.2.1"


@pytest.mark.normal
async def test_start_ready(sys_pki, grpc_stack) -> None:
    """UC-01-S01：有效配置启动后 READY；启动过程不产生任何现场写入。"""
    health = grpc_stack.service.health()
    assert health.state.value == "READY"
    assert health.ready is True
    assert health.pending_commands == 0
    # 真实 SQLite 文件由组合根创建；零命令证明启动不向 PLC 写入。
    assert grpc_stack.db_path.exists()
    assert health.control_accepted_total == 0
    common, _commander = grpc_stack.stubs(sys_pki, client="viewer")
    status = await common.GetServiceStatus(common_pb2.Empty(), timeout=5)
    assert status.health == "HEALTHY"


@pytest.mark.exception
async def test_start_invalid_config(tmp_path) -> None:
    """UC-01-S02：引用失效的配置启动必须失败，不得进入 READY。"""
    root = tmp_path / "bad" / "config"
    copytree(_REPO_CONFIGS / "instances", root / "instances")
    copytree(_REPO_CONFIGS / "templates", root / "templates")
    (tmp_path / "bad" / "state").mkdir(parents=True)
    service_yaml = tmp_path / "bad" / "commander.yaml"
    service_yaml.write_text(
        f"""
schema_version: 1
file_version: 1.0.0
config_sources:
  instances:
    devices: config/instances/devices.yaml
  templates:
    root: config/templates
repository:
  sqlite_path: {tmp_path / 'bad' / 'state' / 'cmd.db'}
audit:
  nats_servers: [nats://127.0.0.1:1]
  stream: CMD_SYS_INVALID
  subject: commander.system.invalid
shutdown:
  grace_seconds: 1
""",
        encoding="utf-8",
    )
    # pathname 本身存在（绕过 build 期 fail-fast），但引用闭包断裂：
    # 删除被设备引用的版本化设备模型，start 期共享配置加载必须拒绝 READY。
    referenced = root / "templates" / "device_models" / "wtg_tz2mw_1.0.0.yaml"
    assert referenced.exists()
    referenced.unlink()
    registry = ProtocolRegistry()
    registry.register("modbus", ModbusDriver)
    service = build_commander(service_config=service_yaml, protocol_registry=registry)
    with pytest.raises(Exception):  # noqa: B017, BLE001
        await service.start()
    assert service.health().state.value == "FAILED"
    assert service.health().ready is False


@pytest.mark.normal
async def test_graceful_shutdown(sys_pki, grpc_stack) -> None:
    """UC-06-S01：空闲服务有界停止成功，不报告任何未完成项。"""
    unfinished = await grpc_stack.service.stop()
    assert unfinished == frozenset()
    assert grpc_stack.service.health().state.value == "STOPPED"


def _blackhole_with_short_grace(service_dir: Path) -> None:
    """把 TZ01 指向不可路由地址，并把停止宽限压到 1s。"""
    devices = service_dir / "config" / "instances" / "devices.yaml"
    text = devices.read_text(encoding="utf-8")
    # 精确改写 TZ01 块（其前可能存在 GW 系列设备，不能替换首个 host）。
    prefix, rest = text.split("  TZ01:", 1)
    block, suffix = rest.split("  TZ02:", 1)
    assert "host: 127.0.0.1" in block and "port: 1502" in block
    block = block.replace("host: 127.0.0.1", f"host: {_BLACKHOLE_HOST}", 1)
    devices.write_text(prefix + "  TZ01:" + block + "  TZ02:" + suffix, encoding="utf-8")
    yaml_path = service_dir / "commander.yaml"
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8").replace("grace_seconds: 10", "grace_seconds: 1"),
        encoding="utf-8",
    )


@pytest.mark.timeout
@pytest.mark.parametrize(
    "grpc_stack_prepared",
    [_blackhole_with_short_grace],
    indirect=True,
)
async def test_shutdown_unsettled_io(sys_pki, grpc_stack_prepared) -> None:
    """UC-06-S02：未安全结束的 I/O 不得被虚构成安全退出。

    对不可路由地址的真实 TCP 连接在期限内不可能确认结束；有界停止必须
    把 TZ01 列入未完成项并把服务置于 FAILED，命令也不得被判 io_settled。
    """
    stack = grpc_stack_prepared
    _common, commander = stack.stubs(sys_pki, client="ops")
    devices = await commander.ListDevices(commander_pb2.ListDevicesRequest(), timeout=5)
    revision = next(d.revision for d in devices.devices if d.device_id == "TZ01")
    points = await commander.ListPoints(
        commander_pb2.ListPointsRequest(device_id="TZ01"), timeout=5
    )
    point = next(p for p in points.points if p.writable)
    submitted = await commander.SubmitCommand(
        commander_pb2.SubmitCommandRequest(
            device_id="TZ01",
            point_id=point.point_id,
            value=common_pb2.ScalarValue(sint64_value=1),
            expected_revision=revision,
        ),
        timeout=5,
    )
    assert submitted.accepted is True
    # 让连接真实发出并进入悬挂（SYN 无响应，connect_ms=3000）。
    await asyncio.sleep(0.3)
    pre_stop = await commander.GetCommandResult(
        commander_pb2.GetCommandResultRequest(command_id=submitted.command_id),
        timeout=5,
    )
    assert pre_stop.state != "SUCCEEDED"
    assert pre_stop.io_settled is False
    unfinished = await stack.service.stop()
    assert "TZ01" in unfinished
    assert stack.service.health().state.value == "FAILED"


@pytest.mark.recovery
async def test_restart_new_config(sys_pki, tmp_path) -> None:
    """UC-07-S01：停止后改磁盘配置，二次启动重新校验、真实报告新生效版本。"""
    stack, server, service = await build_stack(
        tmp_path,
        sys_pki,
        label="system-restart",
        stream_prefix="CMD_SYS",
    )
    try:
        snapshots = stack.snapshots
        runtime_before = await snapshots.get(
            common_pb2.GetConfigSnapshotRequest(scope=common_pb2.SNAPSHOT_SCOPE_RUNTIME)
        )
        devices_name = next(
            f.relative_path
            for f in runtime_before.files
            if f.relative_path.endswith("instances/devices.yaml")
        )
        before = yaml.safe_load(
            bytes(next(f for f in runtime_before.files if f.relative_path == devices_name).content)
        )
        old_port = before["devices"]["TZ05"]["endpoint"]["port"]
        new_port = old_port + 4000
        await server.stop(grace=5)
        assert await service.stop() == frozenset()

        # 模拟外部配置变更：TZ05 端口改写（仅磁盘，不触碰运行时）。
        devices = stack.yaml_path.parent / "config" / "instances" / "devices.yaml"
        text = devices.read_text(encoding="utf-8")
        marker = "  TZ05:"
        head, tail = text.split(marker, 1)
        tail = tail.replace(f"port: {old_port}", f"port: {new_port}", 1)
        devices.write_text(head + marker + tail, encoding="utf-8")

        registry = ProtocolRegistry()
        registry.register("modbus", ModbusDriver)
        service2 = build_commander(service_config=stack.yaml_path, protocol_registry=registry)
        await service2.start()
        try:
            assert service2.health().state.value == "READY"
            # 重启不重发旧命令：内存接受计数从新进程生命周期归零。
            assert service2.health().control_accepted_total == 0
            runtime_after = await ConfigSnapshots(service2, stack.yaml_path).get(
                common_pb2.GetConfigSnapshotRequest(scope=common_pb2.SNAPSHOT_SCOPE_RUNTIME)
            )
            after = yaml.safe_load(
                bytes(
                    next(f for f in runtime_after.files if f.relative_path == devices_name).content
                )
            )
            assert after["devices"]["TZ05"]["endpoint"]["port"] == new_port
            # 其余设备配置保持不变。
            assert (
                after["devices"]["TZ01"]["endpoint"]["port"]
                == before["devices"]["TZ01"]["endpoint"]["port"]
            )
        finally:
            await service2.stop()
    finally:
        # service 已在主体中停止；幂等停止不报错。
        if service.health().state.value != "STOPPED":
            await service.stop()
