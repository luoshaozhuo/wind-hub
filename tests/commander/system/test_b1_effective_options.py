"""B1 复测：型号协议参数合成 / 端口三级回退 / reload 新会话 / 指纹授权。

关键约束：word_order 等参数**只配在型号模板**，实例 overrides 不绕过。
真实 pymodbus server 的寄存器回读为现场证据；若仍按 little_endian 发帧
或端口回退失败，命令只会 CONNECT_FAILED 或回读乱码。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from shutil import copytree
from uuid import uuid4

import pytest

from commander.application.config_manager import CommanderConfigManager
from commander.application.service_lifecycle import CommanderService
from commander.domain.command import CommandSpec
from commander.infrastructure.assembly import build_commander
from commander.infrastructure.service_settings import load_service_settings
from core.application.protocol_contract import ProtocolWrite
from core.domain import ConnectionEndpoint
from core.infrastructure.protocol.modbus import ModbusDriver
from tests.core.protocol.conftest import make_point, make_table
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.process import free_port

from ..integration.test_nats_jetstream_real import _connect
from .conftest import _BUSINESS_POINTS, _REPO_CONFIGS, _SETPOINT_TABLE
from .test_service_e2e_real_stack import _registry

pytestmark = [
    pytest.mark.system,
    pytest.mark.modbus,
    pytest.mark.real_service,
]

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
  nats_servers: ["{nats_url}"]
  stream: {stream}
  subject: {subject}
"""


def _build_tree(
    tmp_root: Path,
    *,
    devices_yaml: str,
    model_yaml: str,
    nats_url: str = "nats://127.0.0.1:1",
    stream: str = "S",
    subject: str = "s",
) -> tuple[Path, Path]:
    root = tmp_root / "config"
    copytree(_REPO_CONFIGS / "instances", root / "instances")
    copytree(_REPO_CONFIGS / "templates", root / "templates")
    (root / "instances" / "devices.yaml").write_text(devices_yaml, encoding="utf-8")
    model_file = root / "templates/device_models/modbus_wtg_1.0.0.yaml"
    model_file.write_text(model_yaml, encoding="utf-8")
    (root / "templates/point_tables/modbus_wtg_1.0.0.yaml").write_text(
        _SETPOINT_TABLE, encoding="utf-8"
    )
    (root / "templates/business_points.yaml").write_text(_BUSINESS_POINTS, encoding="utf-8")
    db_path = tmp_root / "state" / "commander.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    service_yaml = tmp_root / "commander.yaml"
    service_yaml.write_text(
        _SERVICE_YAML.format(
            sqlite_path=str(db_path), nats_url=nats_url, stream=stream, subject=subject
        ),
        encoding="utf-8",
    )
    return root, service_yaml


def _model_yaml(*, port: int | None, word_order: str = "big_endian") -> str:
    defaults = f"\nendpoint_defaults:\n  port: {port}" if port is not None else ""
    return (
        "schema_version: 1\n"
        "template_id: modbus_wtg\n"
        "file_version: 1.0.0\n"
        "name: B1 turbine\n"
        "device_type_id: turbine\n"
        "protocol:\n  id: modbus_tcp\n  version: 1.0.0\n"
        "point_table:\n  id: modbus_wtg\n  version: 1.0.0\n"
        "task:\n  id: turbine_all\n  version: 1.0.0\n"
        "protocol_options:\n"
        "  slave_id: 1\n"
        f"  word_order: {word_order}\n"
        f"{defaults}\n"
    )


def _devices_yaml(*, port: int | None, word_order: str | None) -> str:
    endpoint = "    endpoint:\n      host: 127.0.0.1\n"
    if port is not None:
        endpoint += f"      port: {port}\n"
    overrides = "    overrides: {}\n"
    if word_order is not None:
        overrides = (
            "    overrides:\n      protocol_options:\n" f"        word_order: {word_order}\n"
        )
    return (
        "schema_version: 1\nfile_version: 1.0.0\ndevices:\n  WT-B1:\n"
        "    model:\n      id: modbus_wtg\n      version: 1.0.0\n"
        f"{endpoint}{overrides}"
    )


def _config(service: CommanderService) -> CommanderConfigManager:
    config = service._config  # noqa: SLF001
    assert config is not None
    return config


def _manager(root: Path, service_yaml: Path) -> CommanderConfigManager:
    async def _no_quiesce(_devices: frozenset[str]) -> frozenset[str]:
        return frozenset()

    manager = CommanderConfigManager(
        quiesce=_no_quiesce,
        settings_loader=lambda: load_service_settings(service_yaml),
    )
    manager.start()
    return manager


def _verifier(port: int, word_order: str) -> ModbusDriver:
    table = make_table(
        "modbus",
        make_point(
            "setpoint_power",
            register_type="holding",
            address=200,
            data_type="float32",
        ),
    )
    return ModbusDriver(
        ConnectionEndpoint("127.0.0.1", port),
        table,
        {"word_order": word_order, "timeout": 2.0},
    )


async def _start_real_service(
    tmp_path: Path,
    modbus_port: int,
    nats_url: str,
    *,
    device_word_order: str | None = None,
    model_word_order: str = "big_endian",
    model_port: int | None = None,
    device_port: int | None = None,
) -> tuple[CommanderService, Path, str, str]:
    stream = f"CMD_B1_{uuid.uuid4().hex[:12]}"
    subject = f"commander.b1.{uuid.uuid4().hex[:8]}"
    config_root, service_yaml = _build_tree(
        tmp_path,
        devices_yaml=_devices_yaml(port=device_port, word_order=device_word_order),
        model_yaml=_model_yaml(
            port=modbus_port if model_port is None else model_port,
            word_order=model_word_order,
        ),
        nats_url=nats_url,
        stream=stream,
        subject=subject,
    )
    bootstrap = await _connect(nats_url)
    await bootstrap.jetstream(timeout=5).add_stream(name=stream, subjects=[subject])
    await bootstrap.drain()
    service = build_commander(
        service_config=service_yaml,
        protocol_registry=_registry(),
    )
    await service.start()
    return service, config_root, stream, subject


async def test_model_only_options_and_model_default_port_drive_real_modbus(
    tmp_path, modbus_server, jetstream_url
) -> None:
    """型号模板携带 word_order + endpoint_defaults.port；实例只给 host。

    上轮 B1 的精确复现布局：实例无 port、无 protocol_options。修复前
    端口回落到 502（CONNECT_FAILED），且 word_order 为 little_endian。
    """
    service, _, _, _ = await _start_real_service(tmp_path, modbus_server.port, jetstream_url)
    try:
        fingerprint = _config(service).device_revision("WT-B1")
        assert fingerprint is not None
        accepted = await service.executor.accept(
            [CommandSpec("WT-B1", "setpoint_power", 42.5, fingerprint)],
            request_id=f"req-b1-{uuid4().hex}",
            caller_id="ops-b1",
        )
        result = await service.executor.wait(accepted[0].command_id)
        assert result is not None
        assert result.state.value == "SUCCEEDED", result.error_code
    finally:
        stopped = await service.stop()
        assert stopped == frozenset()

    verifier = _verifier(modbus_server.port, "big_endian")
    await verifier.connect()
    try:
        sample = await verifier.read_one("setpoint_power")
        assert sample.value == pytest.approx(42.5, abs=0.01)
    finally:
        await verifier.close()


async def test_device_override_wins_over_model_default_real_modbus(tmp_path) -> None:
    """型号默认 big_endian；实例 overrides little_endian 必须实际覆盖。"""
    port = free_port()
    server = ModbusMockServer(port=port)
    await server.start()
    try:
        config_root, service_yaml = _build_tree(
            tmp_path,
            devices_yaml=_devices_yaml(port=None, word_order="little_endian"),
            model_yaml=_model_yaml(port=port, word_order="big_endian"),
        )
        manager = _manager(config_root, service_yaml)
        driver = manager.create_session("WT-B1", _registry())
        assert isinstance(driver, ModbusDriver)
        assert driver._config.word_order == "little_endian"  # noqa: SLF001
        assert driver._config.port == port  # noqa: SLF001
        await driver.connect()
        try:
            reply = await driver.write_one(ProtocolWrite("setpoint_power", 42.5))
            assert reply.outcome.value == "confirmed_success"
        finally:
            await driver.close()

        verifier = _verifier(port, "little_endian")
        await verifier.connect()
        try:
            sample = await verifier.read_one("setpoint_power")
            assert sample.value == pytest.approx(42.5, abs=0.01)
            # 用 big_endian 回读必须是乱码，证明实例覆盖真实生效。
            wrong = _verifier(port, "big_endian")
            await wrong.connect()
            try:
                garbled = await wrong.read_one("setpoint_power")
                assert garbled.value != pytest.approx(42.5, abs=1.0)
            finally:
                await wrong.close()
        finally:
            await verifier.close()
    finally:
        await server.stop()


async def test_port_falls_back_to_protocol_default_502(tmp_path) -> None:
    """实例与型号均不配端口：有效端口落到协议代码默认 502（只构造不连接）。"""
    config_root, service_yaml = _build_tree(
        tmp_path,
        devices_yaml=_devices_yaml(port=None, word_order=None),
        model_yaml=_model_yaml(port=None, word_order="big_endian"),
    )
    manager = _manager(config_root, service_yaml)
    driver = manager.create_session("WT-B1", _registry())
    assert isinstance(driver, ModbusDriver)
    assert driver._config.port == 502  # noqa: SLF001
    assert driver._config.word_order == "big_endian"  # noqa: SLF001
    assert driver._config.slave_id == 1  # noqa: SLF001


async def test_model_option_change_rotates_fingerprint_and_reload_routes_new_session(
    tmp_path, jetstream_url
) -> None:
    """型号模板端口/参数变化 → 指纹轮换；reload 后命令走新物理对端。"""
    port_a = free_port()
    port_b = free_port()
    server_a = ModbusMockServer(port=port_a)
    server_b = ModbusMockServer(port=port_b)
    await server_a.start()
    await server_b.start()
    try:
        service, config_root, _, _ = await _start_real_service(tmp_path, port_a, jetstream_url)
        try:
            old_reference = _config(service).device_revision("WT-B1")
            assert old_reference is not None
            accepted = await service.executor.accept(
                [CommandSpec("WT-B1", "setpoint_power", 11.0, old_reference)],
                request_id=f"req-b1-{uuid4().hex}",
                caller_id="ops-b1",
            )
            first = await service.executor.wait(accepted[0].command_id)
            assert first is not None and first.state.value == "SUCCEEDED"

            # 修改型号模板：默认端口 A → B。
            model_file = config_root / "templates/device_models/modbus_wtg_1.0.0.yaml"
            model_file.write_text(
                _model_yaml(port=port_b, word_order="big_endian"),
                encoding="utf-8",
            )
            report = await service.reload()
            assert "WT-B1" in report.changed
            new_reference = _config(service).device_revision("WT-B1")
            assert new_reference is not None and new_reference != old_reference

            # 旧 config_reference 在新修订下必须失去授权。
            assert not _config(service).authorized(
                CommandSpec("WT-B1", "setpoint_power", 1.0, old_reference)
            )

            accepted2 = await service.executor.accept(
                [CommandSpec("WT-B1", "setpoint_power", 22.0, new_reference)],
                request_id=f"req-b1-{uuid4().hex}",
                caller_id="ops-b1",
            )
            second = await service.executor.wait(accepted2[0].command_id)
            assert second is not None and second.state.value == "SUCCEEDED"
        finally:
            assert await service.stop() == frozenset()

        # 第二拍必须物理到达 server B；A 只收到第一拍。
        verifier_a = _verifier(port_a, "big_endian")
        verifier_b = _verifier(port_b, "big_endian")
        await verifier_a.connect()
        await verifier_b.connect()
        try:
            assert (await verifier_a.read_one("setpoint_power")).value == pytest.approx(
                11.0, abs=0.01
            )
            assert (await verifier_b.read_one("setpoint_power")).value == pytest.approx(
                22.0, abs=0.01
            )
        finally:
            await verifier_a.close()
            await verifier_b.close()
    finally:
        await server_a.stop()
        await server_b.stop()
