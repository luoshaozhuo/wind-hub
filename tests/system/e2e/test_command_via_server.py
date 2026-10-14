"""System E2E：REST → Server → Commander → Modbus → Device 命令链路验收。

覆盖《软件交付与CI质量门禁规范》§4.3 的关键链路::

    API/CLI → Server → Commander → Protocol → Device → Result

HTTP 客户端经 wind-hub-server 的 ``POST /api/v1/devices/{id}/commands``
发起写命令；Server 内部经 CommanderGrpcClient 调用 wind-hub-commander
subprocess 的 ``WritePoint``；Commander 经真实 Modbus TCP 写入从站。
全部断言落在系统边界：REST 响应 + **独立** Modbus 回读（从站寄存器值、
写请求计数），不读取任何进程内部状态。

错误映射现状（如实记录，非目标契约）：Commander 进程不可达时 gRPC
``AioRpcError`` 经统一错误处理器返回 ``500 INTERNAL_ERROR``；Commander
可达但命令失败（如未知设备）时返回 ``200`` 且 ``success=false``。
"""

from __future__ import annotations

import struct

import pytest

from tests.system.conftest import FullStack

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

#: 命令目标点：``setpoint.power`` @ holding 200（float32，big-endian，
#: 与 write_functional_config 的点表及 ModbusMockServer 布局一致）。
_COMMAND_POINT = "setpoint.power"
_COMMAND_ADDR = 200


def _decode_float32(registers: list[int]) -> float:
    """按 big-endian float32 解码两个 16 位寄存器。"""
    return struct.unpack(">f", struct.pack(">HH", registers[0], registers[1]))[0]


async def _send_command(
    stack: FullStack,
    *,
    device_id: str = "modbus-1",
    point_id: str = _COMMAND_POINT,
    value: float,
    command_id: str | None = None,
) -> dict:
    """经 REST 发起一次写命令并返回响应 JSON（断言 200 由调用方负责）。"""
    payload: dict[str, object] = {"point_id": point_id, "value": value, "timeout": 5.0}
    if command_id is not None:
        payload["command_id"] = command_id
    response = await stack.http.post(f"/api/v1/devices/{device_id}/commands", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


class TestCommandViaServer:
    async def test_write_command_reaches_device_and_readback(
        self, full_stack: FullStack
    ) -> None:
        """REST 写命令必须真正改变从站寄存器，且响应携带一致回读。"""
        result = await _send_command(
            full_stack, value=66.6, command_id="cmd-e2e-write-1"
        )

        assert result["success"] is True, result["error"]
        assert result["command_id"] == "cmd-e2e-write-1"
        # Server 写后即时回读（经 Commander read_point）。
        assert result["readback"] == pytest.approx(66.6)
        assert result["readback_quality"] == "good"

        # 独立回读：不经过 Server/Commander，直接查从站寄存器。
        registers = await full_stack.modbus.read_holding(1, _COMMAND_ADDR, 2)
        assert _decode_float32(registers) == pytest.approx(66.6)

    async def test_same_command_id_executes_once(self, full_stack: FullStack) -> None:
        """command_id 幂等：重复请求命中 Commander 结果缓存，设备只写一次。"""
        full_stack.modbus.reset_write_count()

        first = await _send_command(
            full_stack, value=77.7, command_id="cmd-e2e-idem-1"
        )
        repeat = await _send_command(
            full_stack, value=77.7, command_id="cmd-e2e-idem-1"
        )

        assert first["success"] is True
        assert repeat["success"] is True
        assert repeat["command_id"] == "cmd-e2e-idem-1"
        assert full_stack.modbus.write_count == 1

        # 不同 command_id 是新命令——必须真正下发。
        other = await _send_command(full_stack, value=88.8, command_id="cmd-e2e-idem-2")
        assert other["success"] is True
        assert full_stack.modbus.write_count == 2
        registers = await full_stack.modbus.read_holding(1, _COMMAND_ADDR, 2)
        assert _decode_float32(registers) == pytest.approx(88.8)

    async def test_unknown_device_returns_failed_result(
        self, full_stack: FullStack
    ) -> None:
        """Commander 可达但设备不存在：200 + success=false，且不产生设备写。"""
        full_stack.modbus.reset_write_count()

        result = await _send_command(
            full_stack, device_id="ghost-device", value=1.0, command_id="cmd-e2e-ghost"
        )

        assert result["success"] is False
        assert result["command_id"] == "cmd-e2e-ghost"
        assert "ghost-device" in (result["error"] or "")
        assert full_stack.modbus.write_count == 0

    async def test_commander_unavailable_returns_error_envelope(
        self, full_stack: FullStack
    ) -> None:
        """Commander 进程不可达：非 2xx 统一错误信封，绝不伪报成功。"""
        full_stack.modbus.reset_write_count()
        assert full_stack.commander.is_running()
        full_stack.commander.terminate()

        response = await full_stack.http.post(
            "/api/v1/devices/modbus-1/commands",
            json={"point_id": _COMMAND_POINT, "value": 1.0, "timeout": 5.0},
        )

        # 现行映射：上游 gRPC 不可用 → 500 INTERNAL_ERROR 统一信封。
        assert response.status_code == 500, response.text
        body = response.json()
        assert body["error"]["code"] == "INTERNAL_ERROR"
        assert full_stack.modbus.write_count == 0
