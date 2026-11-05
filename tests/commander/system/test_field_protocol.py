"""§14.3/§14.4 现场协议系统测试（field 场景）。

设计约束（§14.1）：现场写入只允许在**授权的隔离环境**对真实 PLC 执行，
协议模拟器不得替代最终验收。本文件在无授权窗口（环境变量
``COMMANDER_FIELD_WINDOW`` 未置位）或无现场端点信息时一律 SKIP，
验收记录为 NOT_RUN，绝不以模拟器结果冒充现场 PASS。
"""

from __future__ import annotations

import os

import pytest

pytestmark = [pytest.mark.system, pytest.mark.reliability, pytest.mark.field]


def _field_window() -> dict[str, str]:
    return {
        "window": os.environ.get("COMMANDER_FIELD_WINDOW", ""),
        "endpoint": os.environ.get("COMMANDER_FIELD_ENDPOINT", ""),
        "device_id": os.environ.get("COMMANDER_FIELD_DEVICE_ID", ""),
        "point_id": os.environ.get("COMMANDER_FIELD_POINT_ID", ""),
    }


@pytest.mark.field
def test_field_write_real_device() -> None:
    """授权窗口下对真实设备写入、超时与安全结束的现场验收（默认 NOT_RUN）。"""
    window = _field_window()
    if window["window"] != "1" or not all(
        window[key] for key in ("endpoint", "device_id", "point_id")
    ):
        pytest.skip(
            "NOT_RUN: 无授权现场窗口（需 COMMANDER_FIELD_WINDOW=1 与 "
            "COMMANDER_FIELD_ENDPOINT/DEVICE_ID/POINT_ID）；不得以模拟器替代"
        )
    # 授权环境下由现场工程师补充执行记录：真实 PLC 写入、超时裁决、
    # io_settled 证据采集与隔离结束确认。
    pytest.skip("NOT_RUN: 现场执行流程待授权窗口下按 §12.1/§12.3 人工实施")
