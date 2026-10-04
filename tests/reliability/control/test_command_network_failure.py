"""Reliability：控制命令在网络异常下的交付语义。

风电现场最危险的一类故障是「命令可能已执行但结果未知」。本文件用
真实全链路（REST → Server → Commander → 代理 → Modbus 从站）固定当前
系统语义：

- CMD-01 设备离线时写：明确 ``success=false``，绝不伪报成功、绝不下发；
- CMD-02 响应丢失（设备已执行）：客户端观察到的是 timeout 类失败，
  系统**没有** unknown 状态、也绝不自动重发（从站 write_count 保持 1）；
- CMD-03 同 command_id 重试：命中 Commander 幂等缓存（失败结果同样
  缓存），设备不重复执行；
- CMD-04 不同 command_id 相同写值：视为独立命令，恢复后正常下发。

响应丢失由 ``WriteResponseDropProxy`` 在 TCP 层按帧注入——从站真实执行
写入（``write_count`` 与独立回读证明），只是响应帧不到达客户端。
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.reliability.control.conftest import ProxiedStack
from tests.system.conftest import FullStack

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

_COMMAND_POINT = "setpoint.power"
_COMMAND_ADDR = 200


def _decode_float32(registers: list[int]) -> float:
    return struct.unpack(">f", struct.pack(">HH", registers[0], registers[1]))[0]


async def _send_command(
    http,
    *,
    device_id: str = "modbus-1",
    value: float,
    command_id: str,
) -> dict:
    response = await http.post(
        f"/api/v1/devices/{device_id}/commands",
        json={"point_id": _COMMAND_POINT, "value": value, "timeout": 5.0,
              "command_id": command_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestWriteWhileOffline:
    async def test_write_fails_explicitly_and_recovers(
        self, full_stack: FullStack
    ) -> None:
        """CMD-01：设备掉线时 REST 写明确失败；设备恢复后写恢复。"""
        modbus: ModbusMockServer = full_stack.modbus
        # ---- 故障前正常：一次成功写 ----
        ok = await _send_command(full_stack.http, value=55.5, command_id="cmd-pre")
        assert ok["success"] is True
        modbus.reset_write_count()

        # ---- 故障：从站停止 ----
        await modbus.stop()

        result = await _send_command(full_stack.http, value=66.6, command_id="cmd-offline")
        assert result["success"] is False, "write to offline device must not fake success"
        assert result["error"], "failure must carry an explicit error"
        assert modbus.write_count == 0, "offline write must not reach the device"

        # ---- 恢复：控制面与数据面恢复 ----
        await modbus.start()
        recovered = await _send_command(
            full_stack.http, value=77.7, command_id="cmd-recovered"
        )
        assert recovered["success"] is True
        assert modbus.write_count == 1


class TestResponseLostAfterExecution:
    async def test_response_lost_is_not_retried_and_retry_is_idempotent(
        self, proxied_stack: ProxiedStack
    ) -> None:
        """CMD-02/03/04：响应丢失语义 + command_id 重试幂等。"""
        stack = proxied_stack
        # ---- 故障前正常：经代理的成功写 ----
        ok = await _send_command(stack.http, value=55.5, command_id="cmd-pre-drop")
        assert ok["success"] is True
        stack.modbus.reset_write_count()

        # ---- CMD-02：从站执行写但响应帧被丢弃 ----
        stack.proxy.drop_write_responses = True
        lost = await _send_command(stack.http, value=66.6, command_id="cmd-drop-1")

        # 客户端看到的是明确的失败（timeout 类错误）；系统没有 unknown 状态。
        assert lost["success"] is False
        error_text = (lost["error"] or "").lower()
        assert "timeout" in error_text or "failed" in error_text, lost["error"]
        assert stack.proxy.dropped_responses >= 1, "fault injection did not take effect"

        # 设备确实已执行：写请求到达且寄存器已改变。
        assert stack.modbus.write_count == 1
        registers = await stack.modbus.read_holding(1, _COMMAND_ADDR, 2)
        assert _decode_float32(registers) == pytest.approx(66.6)

        # 结果未知的命令绝不自动重发。
        await asyncio.sleep(3.0)
        assert stack.modbus.write_count == 1, (
            "system retried an uncertain write on its own — duplicate execution risk"
        )
        stack.proxy.drop_write_responses = False

        # ---- CMD-03：同 command_id 重试命中幂等缓存，设备不重复执行 ----
        retry = await _send_command(stack.http, value=66.6, command_id="cmd-drop-1")
        assert retry["success"] is False, "cached failure must be returned as-is"
        assert retry["command_id"] == "cmd-drop-1"
        assert stack.modbus.write_count == 1, (
            "same command_id executed twice on the device"
        )

        # ---- CMD-04：不同 command_id 是独立命令，链路恢复后正常下发 ----
        fresh = await _send_command(stack.http, value=66.6, command_id="cmd-drop-2")
        assert fresh["success"] is True
        assert stack.modbus.write_count == 2
