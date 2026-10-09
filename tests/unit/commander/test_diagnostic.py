"""新 Commander 诊断服务单元测试（网络探测注入 fake）。"""

from __future__ import annotations

import pytest

from commander.application.diagnostic import (
    CommanderDiagnosticService,
    DiagnosticCode,
)
from commander.application.errors import CommandError
from commander.application.runtime import CommanderRuntime
from tests.support.new_commander import FakeRegistry, make_commander_config


def _service(
    registry: FakeRegistry,
    *,
    ping_ok: bool = True,
    tcp_ok: bool = True,
) -> CommanderDiagnosticService:
    runtime = CommanderRuntime(
        make_commander_config(),
        config_hash="hash-a",
        protocol_registry=registry,  # type: ignore[arg-type]
    )
    return CommanderDiagnosticService(
        runtime,
        ping=lambda host, timeout: _const(ping_ok),
        tcp_connect=lambda host, port, timeout: _const(tcp_ok),
    )


async def _const(value: bool) -> bool:
    return value


async def test_verify_device_all_stages_ok():
    registry = FakeRegistry()
    service = _service(registry)
    result = await service.verify_device("dev1")
    assert result.ok
    assert [stage.name for stage in result.stages] == [
        "network",
        "transport",
        "protocol",
    ]
    assert all(stage.ok for stage in result.stages)


async def test_verify_device_ping_failure_degrades_protocol_check():
    registry = FakeRegistry()
    service = _service(registry, ping_ok=False, tcp_ok=False)
    result = await service.verify_device("dev1")
    assert not result.ok
    stages = {stage.name: stage for stage in result.stages}
    assert stages["network"].code is DiagnosticCode.PING_FAILED
    assert stages["transport"].code is DiagnosticCode.TCP_PORT_UNREACHABLE
    assert stages["protocol"].code is DiagnosticCode.PROTOCOL_CONNECT_FAILED


async def test_verify_device_unknown_device():
    registry = FakeRegistry()
    service = _service(registry)
    with pytest.raises(CommandError, match="unknown device"):
        await service.verify_device("ghost")


async def test_verify_point_non_ads_reads_value():
    registry = FakeRegistry()
    service = _service(registry)
    registry.instances[0].read_values["p1"] = 10.0
    result = await service.verify_point("dev1", "p1")
    assert result.ok
    assert result.raw_value == 10.0
    assert result.engineering_value == 10.0
    assert result.protocol == "modbus"
    # 非 ADS：解析事实即配置地址
    assert result.resolved_address is not None
    assert result.resolved_address["register_type"] == "holding"


async def test_verify_point_unknown_point():
    registry = FakeRegistry()
    service = _service(registry)
    with pytest.raises(CommandError, match="unknown point"):
        await service.verify_point("dev1", "ghost")


async def test_verify_points_by_group():
    registry = FakeRegistry()
    service = _service(registry)
    result = await service.verify_points("dev1", point_group="g")
    assert result.checked == 1
    assert result.passed == 1
    assert result.ok


async def test_verify_points_unknown_group_rejected():
    registry = FakeRegistry()
    service = _service(registry)
    with pytest.raises(CommandError, match="point group"):
        await service.verify_points("dev1", point_group="ghost")


class _FakeProbe:
    """ADS SymbolProbe 替身：解析结果与配置 index 地址不一致。"""

    def __init__(self) -> None:
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def resolve(self, point):  # noqa: ANN001, ANN201
        from commander.application.diagnostic import ResolvedAddress

        return ResolvedAddress(
            symbol="MAIN.x",
            index_group=0xF021,
            index_offset=4,
            size=4,
            protocol_type="REAL",
        )


def _ads_service(registry: FakeRegistry) -> CommanderDiagnosticService:
    from commander.application.diagnostic import ResolvedAddress  # noqa: F401

    runtime = CommanderRuntime(
        make_commander_config(
            protocol="ads",
            address={
                "symbol": "MAIN.x",
                "index_group": 0xF020,
                "index_offset": 0,
                "data_type": "REAL",
            },
        ),
        config_hash="hash-a",
        protocol_registry=registry,  # type: ignore[arg-type]
    )
    return CommanderDiagnosticService(
        runtime,
        ping=lambda host, timeout: _const(True),
        tcp_connect=lambda host, port, timeout: _const(True),
        probe_factory=lambda device, endpoint, options: _FakeProbe(),
    )


async def test_ads_mapping_mismatch_reported_when_readable():
    """配置 index 与 PLC symbol 解析不一致：可读时也报 MISMATCH/WARNING（旧语义）。"""
    registry = FakeRegistry()
    service = _ads_service(registry)
    registry.instances[0].read_values["p1"] = 10.0
    result = await service.verify_point("dev1", "p1")
    assert not result.ok
    assert result.code is DiagnosticCode.POINT_MAPPING_MISMATCH


async def test_ads_read_failure_reports_read_failed_despite_mismatch():
    """读取失败（error 非 None）优先于 mismatch——新旧 Commander 一致的行为。"""
    registry = FakeRegistry()
    service = _ads_service(registry)
    registry.instances[0].read_values["p1"] = None  # value null → readable=False
    result = await service.verify_point("dev1", "p1")
    assert not result.ok
    assert result.code is DiagnosticCode.POINT_READ_FAILED
