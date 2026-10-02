"""wind-hub-core 主动验证数据契约单元测试。

这些模型是 Collector 诊断面与 Server 配置验证共用的稳定 schema：
字段默认值、枚举取值与 ``ok`` 聚合语义变化会直接改变控制面输出，
因此以显式断言固化。序列化验证走 ``dataclasses.asdict`` + JSON——
模型本身不提供自定义序列化方法。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from wind_hub_core.validation.models import (
    AddressResolution,
    DeviceProbeTarget,
    DeviceValidationReport,
    PointProbeSpec,
    PointValidationResult,
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
    target = DeviceProbeTarget(device_id="d1", host="127.0.0.1")
    assert target.options == {}
    # default_factory 语义：两个实例不共享同一个 dict。
    other = DeviceProbeTarget(device_id="d2", host="127.0.0.1")
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


def test_point_validation_result_defaults() -> None:
    result = PointValidationResult(
        point_id="p1",
        readable=True,
        code=ValidationCode.OK,
        severity=ValidationSeverity.INFO,
    )
    assert result.message == ""
    assert result.resolved is None


def _report(
    *,
    ping_ok: bool = True,
    port_ok: bool = True,
    protocol_ok: bool = True,
    points: tuple[PointValidationResult, ...] = (),
) -> DeviceValidationReport:
    return DeviceValidationReport(
        device_id="d1",
        point_table="t1",
        ping_ok=ping_ok,
        port_ok=port_ok,
        protocol_ok=protocol_ok,
        code=ValidationCode.OK,
        severity=ValidationSeverity.INFO,
        points=points,
    )


def test_device_validation_report_defaults_to_empty_points() -> None:
    report = _report()
    assert report.points == ()
    assert report.message == ""
    # 空点集下 all() 为 True——ok 只由链路三层决定。
    assert report.ok is True


@pytest.mark.parametrize(
    ("ping_ok", "port_ok", "protocol_ok", "expected"),
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
def test_device_validation_report_ok_tracks_link_stages(
    ping_ok: bool, port_ok: bool, protocol_ok: bool, expected: bool
) -> None:
    assert _report(ping_ok=ping_ok, port_ok=port_ok, protocol_ok=protocol_ok).ok is expected


def test_device_validation_report_ok_requires_all_points_readable() -> None:
    readable = PointValidationResult(
        point_id="p1",
        readable=True,
        code=ValidationCode.OK,
        severity=ValidationSeverity.INFO,
    )
    unreadable = PointValidationResult(
        point_id="p2",
        readable=False,
        code=ValidationCode.POINT_READ_FAILED,
        severity=ValidationSeverity.ERROR,
    )
    assert _report(points=(readable,)).ok is True
    assert _report(points=(readable, unreadable)).ok is False


def test_models_are_frozen() -> None:
    resolved = AddressResolution(point_id="p1", symbol=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.index_group = 1  # type: ignore[misc]
    report = _report()
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.ping_ok = False  # type: ignore[misc]


def test_models_serialize_to_plain_json_compatible_dicts() -> None:
    resolved = AddressResolution(
        point_id="p1",
        symbol="MAIN.x",
        index_group=16448,
        index_offset=100,
        size=4,
        protocol_type="REAL",
    )
    point = PointValidationResult(
        point_id="p1",
        readable=True,
        code=ValidationCode.OK,
        severity=ValidationSeverity.INFO,
        resolved=resolved,
    )
    report = _report(points=(point,))
    payload = dataclasses.asdict(report)
    # asdict 深展开嵌套 dataclass；StrEnum 经 json 默认编码为字符串值。
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)
    assert decoded["device_id"] == "d1"
    assert decoded["code"] == "OK"
    assert decoded["points"][0]["resolved"]["index_group"] == 16448
    assert decoded["points"][0]["severity"] == "info"
