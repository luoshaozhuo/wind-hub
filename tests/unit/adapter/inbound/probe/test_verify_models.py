"""Unit tests for ``cli/probe/verify_models.py`` — 点表验证结果模型。"""

from __future__ import annotations

from wind_hub.adapter.inbound.cli.probe.verify_models import (
    DeviceVerifyResult,
    PointVerifyResult,
    VerifyResult,
)


def _point(
    point_id: str,
    *,
    ok: bool = True,
    quality: str | None = "good",
    error: str | None = None,
) -> PointVerifyResult:
    return PointVerifyResult(
        point_id=point_id, ok=ok, value=1.0 if ok else None, quality=quality, error=error
    )


def test_point_verify_result_creation() -> None:
    point = PointVerifyResult(point_id="rotor.speed", ok=True, value=12.5, quality="good")
    assert point.error is None
    assert point.quality_bad is False
    assert point.to_dict() == {
        "point_id": "rotor.speed",
        "ok": True,
        "value": 12.5,
        "quality": "good",
        "error": None,
    }


def test_point_verify_result_quality_bad() -> None:
    """读成功但 quality=bad → quality_bad；读失败不算 BAD 质量。"""
    bad = _point("p1", ok=True, quality="bad")
    assert bad.quality_bad is True
    failed = _point("p2", ok=False, quality=None, error="timeout")
    assert failed.quality_bad is False
    good = _point("p3", ok=True, quality="good")
    assert good.quality_bad is False


def test_device_verify_result_summary_properties() -> None:
    device = DeviceVerifyResult(
        device_id="wtg-001",
        protocol="iec104",
        connect_ok=True,
        connect_error=None,
        points=[
            _point("p1"),
            _point("p2", quality="bad"),
            _point("p3", ok=False, quality=None, error="timeout"),
        ],
    )
    assert device.total == 3
    assert device.ok_count == 2
    assert device.fail_count == 1
    assert device.bad_quality_count == 1
    payload = device.to_dict()
    assert payload["device_id"] == "wtg-001"
    assert payload["connect_ok"] is True
    assert payload["total"] == 3
    assert payload["fail_count"] == 1
    assert len(payload["points"]) == 3


def test_verify_result_summary_properties() -> None:
    result = VerifyResult(
        devices=[
            DeviceVerifyResult(
                device_id="d1",
                protocol="modbus",
                connect_ok=True,
                connect_error=None,
                points=[_point("p1"), _point("p2", quality="bad")],
            ),
            DeviceVerifyResult(
                device_id="d2",
                protocol="iec104",
                connect_ok=False,
                connect_error="连接超时",
                points=[_point("p3", ok=False, quality=None, error="连接超时")],
            ),
        ],
        duration_ms=123.4,
    )
    assert result.total_points == 3
    assert result.total_ok == 2
    assert result.total_fail == 1
    assert result.total_bad_quality == 1
    payload = result.to_dict()
    assert payload["summary"] == {
        "total_devices": 2,
        "total_points": 3,
        "total_ok": 2,
        "total_fail": 1,
        "total_bad_quality": 1,
        "duration_ms": 123.4,
    }
    assert len(payload["devices"]) == 2
