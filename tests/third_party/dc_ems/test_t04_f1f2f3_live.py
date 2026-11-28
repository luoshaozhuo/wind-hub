"""T04 F1/F2/F3 生产整改的真实跨系统回归。

- F1：物理 fault_status 0/1 经显式 int01_to_bool 抵达目标 Gateway 与 DCEMS。
- F2：Delivery 人工 STOP/START 全链（停止后不泄漏、启动后恢复）。
- F3：单机注入停止 >5s → 目标 BAD/STALE，其余九机 GOOD/CURRENT；
      恢复注入后回到 CURRENT；并记录 DCEMS 对冻结值的实际反应。
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from tests.third_party.dc_ems.test_t04_unified_live import (
    BASE,
    TARGET_GRPC,
    _cli,
    _dcems_row,
    _gateway_read,
    _generation_id,
    _sample_scalar,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("DCEMS_T04_LIVE") != "1" or os.environ.get("DCEMS_ALLOW_LOCAL_SMOKE") != "1",
    reason="NOT_RUN: DCEMS_T04_LIVE=1 and DCEMS_ALLOW_LOCAL_SMOKE=1 required",
)


def _sink_delivery_state(delivery_id: str) -> str:
    result = _cli("sink", "deliveries")
    items = cast(list[dict[str, Any]], result["data"].get("deliveries") or [])
    for item in items:
        if item.get("delivery_id") == delivery_id:
            return str(item.get("actual_state"))
    raise AssertionError(f"delivery {delivery_id} not found")


def _wait_delivery_state(delivery_id: str, expected: str, timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _sink_delivery_state(delivery_id) == expected:
            return
        time.sleep(1.0)
    raise AssertionError(
        f"delivery {delivery_id} did not reach {expected}: {_sink_delivery_state(delivery_id)}"
    )


def _read_freshness(gateway_id: str, variable_id: str, token_env: str) -> Any:
    samples = _gateway_read(gateway_id, TARGET_GRPC, [variable_id], token_env)
    return samples[variable_id]


# ---------------------------------------------------------------- F3 + F2


def test_f3_single_device_stops_updating_and_recovers_with_f2_restart() -> None:
    """单机注入停止 → 仅该机 BAD/STALE；Delivery START 后恢复 CURRENT。"""
    operator = "WIND_HUB_DCE_TARGET_OPERATOR_TOKEN"
    # 基线：十机全部 CURRENT
    for index in range(1, 11):
        sample = _read_freshness(f"dcems-target-{index:02d}", "wind_speed", operator)
        assert sample.freshness == "CURRENT", (index, sample.freshness)

    # F2 人工 STOP：desired STOP，看门狗不得擅自复活
    _cli("sink", "stop", "dcems_wtg01", "--yes")
    _wait_delivery_state("dcems_wtg01", "STOPPED")
    freeze_at = datetime.now(UTC)
    time.sleep(6.0)  # 超过 read_stale_after_ms=5000

    # 仅 WTG01 过期；其余九机保持 CURRENT
    stale = _read_freshness("dcems-target-01", "wind_speed", operator)
    assert stale.quality == "BAD", stale
    assert stale.freshness == "STALE", stale
    assert stale.error_code == "STALE_VALUE", stale
    for index in range(2, 11):
        sample = _read_freshness(f"dcems-target-{index:02d}", "wind_speed", operator)
        assert sample.quality == "GOOD", (index, sample.quality)
        assert sample.freshness == "CURRENT", (index, sample.freshness)

    # Gateway 冻结值仍在（Modbus 无质量字段，DCEMS 继续读到旧值）
    frozen_value = float(_sample_scalar(cast(Any, stale)))
    assert stale.HasField("value")

    # DCEMS 实际反应：comOk 可能仍为 t（协议层无法表达过期），行值保持冻结
    time.sleep(3.0)
    row_a = _dcems_row("WTG01")
    time.sleep(3.0)
    row_b = _dcems_row("WTG01")
    assert row_a["wind_speed"] == row_b["wind_speed"], (row_a, row_b)
    assert row_a["wind_speed"] == pytest.approx(frozen_value, abs=0.05), (
        row_a["wind_speed"],
        frozen_value,
    )
    dcems_reaction = {
        "gateway_quality": stale.quality,
        "gateway_freshness": stale.freshness,
        "gateway_error_code": stale.error_code,
        "modbus_served_value": frozen_value,
        "dcems_com_state": row_b["com"],
        "dcems_value_frozen": True,
        "freeze_started_at": freeze_at.isoformat(),
        "note": "DCEMS/Modbus 无法表达过期；comOk 与行继续更新但值冻结",
    }
    out = Path("docs/audit/dcems_t04_f1_f2_f3_retest_20261128_evidence/f3")
    out.mkdir(parents=True, exist_ok=True)
    (out / "dcems-reaction-to-frozen-value.json").write_text(
        json.dumps(dcems_reaction, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # F2 人工 START：恢复注入 → freshness 回到 CURRENT
    _cli("sink", "start", "dcems_wtg01", "--yes")
    _wait_delivery_state("dcems_wtg01", "RUNNING")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        sample = _read_freshness("dcems-target-01", "wind_speed", operator)
        if sample.freshness == "CURRENT" and sample.quality == "GOOD":
            break
        time.sleep(1.0)
    else:
        raise AssertionError(f"WTG01 did not recover to CURRENT: {sample}")
    # 恢复后值必须解冻（新样本时间戳晚于冻结点）
    recovered_ts = datetime.fromtimestamp(
        sample.sample_timestamp.seconds + sample.sample_timestamp.nanos / 1e9, tz=UTC
    )
    assert recovered_ts > freeze_at, (recovered_ts, freeze_at)


# ---------------------------------------------------------------- F1


def test_f1_fault_status_zero_one_converts_end_to_end() -> None:
    """物理 fault_status 0/1 → int01_to_bool → DCEMS 故障位；随后恢复 Generation。"""
    operator = "WIND_HUB_DCE_TARGET_OPERATOR_TOKEN"
    generation_id = _generation_id()

    # 基线：源 fault_status=0 → 目标 False/GOOD，DCEMS f
    source0 = _gateway_read(
        "dcems-source-01",
        "127.0.0.1:52073",
        ["fault_status"],
        "WIND_HUB_DCE_GATEWAY_OPERATOR_TOKEN",
    )["fault_status"]
    assert source0.quality == "GOOD"
    assert int(_sample_scalar(cast(Any, source0))) == 0
    target0 = _read_freshness("dcems-target-01", "device_fault_active", operator)
    assert target0.quality == "GOOD"
    assert _sample_scalar(cast(Any, target0)) is False
    assert _dcems_row("WTG01")["fault"] == "f"

    # 故障刺激：风速均值抬到切出值（25 m/s）之上，触发粘性超速/切出跳闸
    _cli(
        "data-generator",
        "variables",
        "update",
        "--generation-id",
        generation_id,
        "--device-id",
        "WTG01",
        "--request-id",
        "t04-f1-trip-wind",
        "--point-id",
        "wind_speed_mean",
        "--value",
        "26",
        "--yes",
        check=False,
    )
    # 等待 fault_status=1 走完全链
    deadline = time.monotonic() + 60
    target1 = None
    while time.monotonic() < deadline:
        source1 = _gateway_read(
            "dcems-source-01",
            "127.0.0.1:52073",
            ["fault_status"],
            "WIND_HUB_DCE_GATEWAY_OPERATOR_TOKEN",
        )["fault_status"]
        if int(_sample_scalar(cast(Any, source1))) == 1:
            target1 = _read_freshness("dcems-target-01", "device_fault_active", operator)
            if _sample_scalar(cast(Any, target1)) is True:
                break
        time.sleep(1.0)
    else:
        raise AssertionError("fault_status=1 did not reach target device_fault_active=True")
    assert target1.quality == "GOOD"
    assert target1.freshness == "CURRENT"
    assert _sample_scalar(cast(Any, target1)) is True

    # DCEMS 读取结果（经真实 FC02 轮询落库）
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _dcems_row("WTG01")["fault"] == "t":
            break
        time.sleep(1.0)
    else:
        raise AssertionError("DCEMS did not observe fault=true")

    # 隔离：随机机不受影响
    for index in range(2, 11):
        sample = _read_freshness(f"dcems-target-{index:02d}", "device_fault_active", operator)
        assert _sample_scalar(cast(Any, sample)) is False, index

    evidence = {
        "baseline": {"source_fault_status": 0, "target": False, "dcems": "f"},
        "after_trip": {"source_fault_status": 1, "target": True, "dcems": "t"},
        "conversion": "int01_to_bool",
        "isolation_random_devices": "WTG02-10 device_fault_active=False",
        "stimulus": "wind_speed_mean=26 >= cut_out_speed_m_s=25 (weather input)",
    }
    out = Path("docs/audit/dcems_t04_f1_f2_f3_retest_20261128_evidence/f1")
    out.mkdir(parents=True, exist_ok=True)
    (out / "fault-status-e2e.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # 粘性跳闸在 Generation 生命周期内不可恢复：重启 Generation 回到健康基线
    _cli("data-generator", "stop", generation_id, "--yes", check=False)
    started = _cli("data-generator", "start", "t04-unified-ten", "--yes")
    new_id = str(started["data"]["generation_id"])
    assert new_id
    (BASE / "state" / "generation-id.txt").write_text(new_id, encoding="utf-8")
    _cli(
        "data-generator",
        "variables",
        "update",
        "--generation-id",
        new_id,
        "--device-id",
        "WTG01",
        "--request-id",
        "t04-f1-restore-setpoint",
        "--point-id",
        "active_power_setpoint",
        "--value",
        "450000",
        "--yes",
    )
    _cli(
        "data-generator",
        "variables",
        "update",
        "--generation-id",
        new_id,
        "--device-id",
        "WTG01",
        "--request-id",
        "t04-f1-restore-mode",
        "--point-id",
        "control_mode",
        "--value",
        "1",
        "--yes",
    )
    # 恢复后目标故障位回到 False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        sample = _read_freshness("dcems-target-01", "device_fault_active", operator)
        if _sample_scalar(cast(Any, sample)) is False and sample.freshness == "CURRENT":
            break
        time.sleep(1.0)
    else:
        raise AssertionError("target device_fault_active did not clear after Generation restart")
