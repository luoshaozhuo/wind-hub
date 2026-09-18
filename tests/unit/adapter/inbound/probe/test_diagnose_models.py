"""Unit tests for ``cli/probe/diagnose_models.py`` — 诊断数据模型。"""

from __future__ import annotations

from wind_hub.adapter.inbound.cli.probe.diagnose_models import (
    DiagnoseResult,
    DiagnoseStep,
    LocalInfo,
    StepStatus,
)


def test_step_status_three_states() -> None:
    """三态枚举（决策 6），str 枚举值即序列化字符串。"""
    assert [s.value for s in StepStatus] == ["ok", "warn", "fail"]
    assert StepStatus.WARN == "warn"


def test_diagnose_step_creation_defaults() -> None:
    step = DiagnoseStep(name="ARP", status=StepStatus.OK, detail="已解析")
    assert step.suggestion is None
    assert step.skipped is False
    assert step.to_dict() == {
        "name": "ARP",
        "status": "ok",
        "detail": "已解析",
        "suggestion": None,
        "skipped": False,
    }


def test_diagnose_step_skipped_serializes() -> None:
    step = DiagnoseStep(
        name="TCP 2404", status=StepStatus.OK, detail="网络层失败，跳过", skipped=True
    )
    assert step.to_dict()["skipped"] is True


def test_local_info_creation_and_to_dict() -> None:
    info = LocalInfo(
        hostname="wind-hub-host",
        ips=[("192.168.1.100/24", "eth0"), ("172.17.0.2/16", "docker0")],
        default_gateway="192.168.1.1",
    )
    assert info.to_dict() == {
        "hostname": "wind-hub-host",
        "ips": [
            {"ip": "192.168.1.100/24", "interface": "eth0"},
            {"ip": "172.17.0.2/16", "interface": "docker0"},
        ],
        "default_gateway": "192.168.1.1",
    }


def test_diagnose_result_to_dict() -> None:
    result = DiagnoseResult(
        local_info=LocalInfo(hostname="h", ips=[], default_gateway=None),
        device_id="wtg-001",
        device_ip="10.0.1.1",
        device_port=2404,
        protocol="iec104",
        steps=[DiagnoseStep(name="ARP", status=StepStatus.FAIL, detail="无记录")],
        overall=StepStatus.FAIL,
        conclusion="诊断未通过",
        same_subnet=False,
    )
    payload = result.to_dict()
    assert payload["device_id"] == "wtg-001"
    assert payload["device_ip"] == "10.0.1.1"
    assert payload["device_port"] == 2404
    assert payload["protocol"] == "iec104"
    assert payload["same_subnet"] is False
    assert payload["overall"] == "fail"
    assert payload["conclusion"] == "诊断未通过"
    assert payload["steps"][0]["status"] == "fail"
    assert payload["local_info"]["hostname"] == "h"
