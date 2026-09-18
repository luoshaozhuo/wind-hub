"""Unit tests for ``cli/probe/ports_models.py`` — 四态模型与服务映射。"""

from __future__ import annotations

from wind_hub.adapter.inbound.cli.probe.ports_models import (
    PortResult,
    PortScanResult,
    PortState,
    guess_service,
)
from wind_hub.config.ports_config import PortsConfig


def test_port_state_has_four_states() -> None:
    """四态枚举（step20 任务 0.1 新增 UNREACHABLE），值即序列化字符串。"""
    assert [s.value for s in PortState] == ["open", "closed", "timeout", "unreachable"]
    assert PortState.UNREACHABLE == "unreachable"


def test_port_result_creation_and_to_dict() -> None:
    result = PortResult(port=502, state=PortState.OPEN, service_guess="modbus")
    assert result.to_dict() == {"port": 502, "state": "open", "service_guess": "modbus"}


def test_port_result_state_serializes_as_string() -> None:
    # str 枚举：直接比较/序列化都是值
    assert PortState.TIMEOUT == "timeout"
    assert PortResult(port=1, state=PortState.CLOSED).to_dict()["state"] == "closed"


def test_port_scan_result_to_dict() -> None:
    result = PortScanResult(
        ip="10.0.1.1",
        ports=[
            PortResult(port=502, state=PortState.OPEN, service_guess="modbus"),
            PortResult(port=2404, state=PortState.CLOSED, service_guess="iec104"),
        ],
        total=2,
        open_count=1,
    )
    assert result.to_dict() == {
        "ip": "10.0.1.1",
        "total": 2,
        "open_count": 1,
        "ports": [
            {"port": 502, "state": "open", "service_guess": "modbus"},
            {"port": 2404, "state": "closed", "service_guess": "iec104"},
        ],
    }


def test_guess_service_known_ports() -> None:
    assert guess_service(502) == "modbus"
    assert guess_service(2404) == "iec104"
    assert guess_service(48898) == "ads"
    assert guess_service(4840) == "opc-ua"
    assert guess_service(44818) == "ethernet-ip"
    assert guess_service(22) == "ssh"


def test_guess_service_unknown_port_returns_none() -> None:
    assert guess_service(19999) is None


def test_guess_service_with_custom_config() -> None:
    """step24：config 提供时查 config.service_map（可含内置表没有的端口）。"""
    config = PortsConfig(default_ports=[20000], service_map={20000: "dnp3"})
    assert guess_service(20000, config) == "dnp3"
    # config 提供时以 config 为准——内置表有的 22 不在自定义表里则返回 None
    assert guess_service(22, config) is None


def test_guess_service_without_config_uses_builtin() -> None:
    """step24：config 缺省回落内置 SERVICE_MAP（向后兼容）。"""
    assert guess_service(502) == "modbus"
    assert guess_service(19999) is None
