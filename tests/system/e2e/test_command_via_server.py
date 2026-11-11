"""System E2E：REST → Server → Commander → Modbus → Device 命令链路验收。

覆盖《软件交付与CI质量门禁规范》§4.3 的关键链路::

    API/CLI → Server → Commander → Protocol → Device → Result

HTTP 客户端经 wind-hub-server 的 ``POST /api/v1/devices/{id}/commands``
发起写命令；Server 内部经 CommanderGrpcClient（强制 mTLS）调用
wind-hub-commander 的正式 SubmitCommand/GetCommandResult RPC；
Commander 经真实 Modbus TCP 写入从站。
全部断言落在系统边界：REST 响应 + **独立** Modbus 回读（从站寄存器值、
写请求计数），不读取任何进程内部状态。

错误映射契约（两条独立通道，均不得伪报成功）：

- 命令管道的失败以 **200 + 结果信封** 返回——``success=false`` 并携带
  三态 ``outcome``：GetDevice 等提交前失败可确证本次控制未执行，记
  ``CONFIRMED_FAILURE``；提交后结果不确定记 ``UNKNOWN``，不得自动重发。
  OpenAPI 对该端点只声明 200/422，统一 503 处理器适用于让 RpcError
  直接冒泡的路由（如 tasks start），不适用于本结果信封。
- Commander 可达但命令失败（如未知设备）同样返回 ``200`` 且
  ``success=false``，错误文本携带 Commander 的权威拒绝细节。
"""

from __future__ import annotations

import struct

import pytest

from tests.system.conftest import SYSTEM_TASK_ID, FullStack

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

#: 命令目标点：``setpoint_power`` @ holding 200（float32，big-endian，
#: 与 write_functional_config 的点表及 ModbusMockServer 布局一致）。
_COMMAND_POINT = "setpoint_power"
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
    async def test_write_command_reaches_device_and_readback(self, full_stack: FullStack) -> None:
        """REST 写命令必须真正改变从站寄存器，且响应携带一致回读。"""
        # 写后即时回读经 CollectorReadRouter 定位唯一 Collector owner；
        # placement 只在 Task 经 REST 正式 START 后才 ASSIGNED，先启动
        # functional 配置中的真实 Task（内部对 v2 Collector 下发设备级
        # StartTask，task_id == device_id == modbus-1）。
        start = await full_stack.http.post(f"/api/v1/tasks/{SYSTEM_TASK_ID}/start")
        assert start.status_code == 200, start.text

        result = await _send_command(full_stack, value=66.6, command_id="cmd-e2e-write-1")

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

        first = await _send_command(full_stack, value=77.7, command_id="cmd-e2e-idem-1")
        repeat = await _send_command(full_stack, value=77.7, command_id="cmd-e2e-idem-1")

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

    async def test_unknown_device_returns_failed_result(self, full_stack: FullStack) -> None:
        """Commander 可达但设备不存在：200 + success=false，且不产生设备写。"""
        full_stack.modbus.reset_write_count()

        result = await _send_command(
            full_stack, device_id="ghost-device", value=1.0, command_id="cmd-e2e-ghost"
        )

        assert result["success"] is False
        assert result["command_id"] == "cmd-e2e-ghost"
        # GetDevice 权威 NOT_FOUND 发生在 Submit 之前：CONFIRMED_FAILURE
        # 可确证未执行控制；错误文本携带 Commander 的权威拒绝细节
        # （Commander 端固定文案 "device not found"，不含请求方设备 id）。
        assert result["outcome"] == "confirmed_failure"
        assert "device not found" in (result["error"] or "")
        assert full_stack.modbus.write_count == 0

    async def test_commander_unavailable_returns_error_envelope(
        self, full_stack: FullStack
    ) -> None:
        """Commander 进程不可达：结果信封明确失败，绝不伪报成功。"""
        full_stack.modbus.reset_write_count()
        assert full_stack.commander.is_running()
        full_stack.commander.terminate()

        response = await full_stack.http.post(
            "/api/v1/devices/modbus-1/commands",
            json={"point_id": _COMMAND_POINT, "value": 1.0, "timeout": 5.0},
        )

        # 命令管道契约（OpenAPI 仅声明 200/422）：GetDevice 的 UNAVAILABLE
        # 发生在 Submit 之前，被转译为 CONFIRMED_FAILURE 结果信封而非冒泡
        # 到统一 503 处理器——调用方拿到的仍是确定的「未执行控制」结论，
        # 安全断言是 success 不得为 true 且从站零写入。
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["success"] is False
        assert body["outcome"] == "confirmed_failure"
        assert "UNAVAILABLE" in (body["error"] or "")
        assert full_stack.modbus.write_count == 0
