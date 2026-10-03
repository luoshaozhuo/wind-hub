"""wind-hub-core 主动验证数据契约单元测试。

这些模型是 Commander 诊断与协议 Probe 共用的稳定契约；枚举 wire 值、
端点字段和地址解析结果会直接影响控制面输出，因此以显式断言固化。
"""

from __future__ import annotations

import json

import pytest

from wind_hub_core.validation.models import (
    AddressResolution,
    DeviceProbeTarget,
    PointProbeSpec,
    ValidationCode,
    ValidationSeverity,
)


def test_validation_code_values_are_stable() -> None:
    assert ValidationCode.OK == "OK"
    assert ValidationCode.PING_FAILED == "PING_FAILED"
    assert ValidationCode.TCP_PORT_UNREACHABLE == "TCP_PORT_UNREACHABLE"
    assert ValidationCode.PROTOCOL_CONNECT_FAILED == "PROTOCOL_CONNECT_FAILED"
    assert ValidationCode.POINT_RESOLVE_FAILED == "POINT_RESOLVE_FAILED"
    assert ValidationCode.POINT_MAPPING_MISMATCH == "POINT_MAPPING_MISMATCH"
    assert ValidationCode.POINT_READ_FAILED == "POINT_READ_FAILED"
    # StrEnum：str() 与 JSON 序列化均产出 wire 值。
    assert str(ValidationCode.OK) == "OK"
    assert json.dumps(ValidationCode.POINT_READ_FAILED) == '"POINT_READ_FAILED"'


def test_validation_severity_values_are_stable() -> None:
    assert ValidationSeverity.INFO == "info"
    assert ValidationSeverity.WARNING == "warning"
    assert ValidationSeverity.ERROR == "error"


def test_device_probe_target_defaults_to_empty_options() -> None:
    target = DeviceProbeTarget(device_id="d1", host="127.0.0.1", port=801)
    assert target.port == 801
    assert target.options == {}
    # default_factory 语义：两个实例不共享同一个 dict。
    other = DeviceProbeTarget(device_id="d2", host="127.0.0.1", port=801)
    assert target.options is not other.options


def test_point_probe_spec_requires_address() -> None:
    spec = PointProbeSpec(point_id="p1", data_type="float32", address={"symbol": "MAIN.x"})
    assert spec.address == {"symbol": "MAIN.x"}
    with pytest.raises(TypeError):
        PointProbeSpec(point_id="p1", data_type="float32")  # type: ignore[call-arg]


def test_address_resolution_defaults() -> None:
    resolved = AddressResolution(point_id="p1", symbol="MAIN.x")
    assert resolved.index_group is None
    assert resolved.index_offset is None
    assert resolved.size is None
    assert resolved.protocol_type is None
