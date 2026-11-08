"""System E2E：Web 层命令幂等——同一 command_id 的并发/重复写只允许一次到达设备。

N-03 退役了 Commander 旧 ``WritePoint`` RPC：现场控制改走正式
SubmitCommand/GetCommandResult，at-most-once 防重边界上移到 Web Server
的 ``DurableCommandRegistry``（真实 SQLite 落盘）。本文件因此不再直连
Commander 旧 RPC，而是驱动 **生产 DeviceCommandService**：

    DeviceCommandService（真实 SQLite journal）
      → CommanderGrpcClient（生产出站适配器，真实 mTLS）
      → 真实 Commander（build_commander 组合根，仅安全端口）
      → 真实 Modbus TCP 从站

设备侧效果以从站写请求计数 + 保持寄存器独立回读佐证——全部断言在系统
边界上，不读取 Commander 进程内部状态。
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from uuid import uuid4

import pytest

from core.application.protocol_contract import WriteOutcome
from tests.commander.grpc_harness import GrpcStack, Pki, build_pki, build_stack
from tests.fixtures.servers.modbus_server import ModbusMockServer
from web_server.adapter.outbound.grpc.commander import CommanderGrpcClient
from web_server.application.device.command import (
    CommandIdConflictError,
    DeviceCommandService,
)
from web_server.application.device.durable_command import DurableCommandRegistry
from web_server.application.device.sample import PointValue
from web_server.infra.point_store import InMemoryTrendStore

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

_DEVICE_ID = "WT01"
_POINT_ID = "setpoint_power"
_SETPOINT_ADDRESS = 200
_CA_ENV = "WIND_HUB_COMMANDER_CA_CERT"
_CERT_ENV = "WIND_HUB_COMMANDER_CLIENT_CERT"
_KEY_ENV = "WIND_HUB_COMMANDER_CLIENT_KEY"


class _ReadsUnavailable:
    """本栈没有 Collector：读回路径必须失败，但不得影响写入确认语义。"""

    async def read_points(self, device_id: str, point_ids: list[str]) -> list[PointValue]:
        raise RuntimeError("no collector in command idempotency stack")

    async def read_point(self, device_id: str, point_id: str) -> PointValue:
        raise RuntimeError("no collector in command idempotency stack")


@pytest.fixture
def pki(tmp_path: Path) -> Pki:
    return build_pki(tmp_path)


@pytest.fixture
async def grpc_stack(
    tmp_path: Path, pki: Pki, modbus_server: ModbusMockServer
) -> GrpcStack:
    stack, server, service = await build_stack(
        tmp_path,
        pki,
        modbus_port=modbus_server.port,
        label="system-idem",
        stream_prefix="CMD_IDEM",
    )
    try:
        yield stack
    finally:
        await server.stop(grace=5)
        await service.stop()


@pytest.fixture
async def idem_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pki: Pki,
    grpc_stack: GrpcStack,
    modbus_server: ModbusMockServer,
) -> tuple[DeviceCommandService, ModbusMockServer]:
    """真实 SQLite journal + 生产 mTLS 适配器 + 真实从站的幂等验收环境。"""
    monkeypatch.setenv(_CA_ENV, str(pki.tmp / "ca.crt"))
    monkeypatch.setenv(_CERT_ENV, str(pki.tmp / "ops.crt"))
    monkeypatch.setenv(_KEY_ENV, str(pki.tmp / "ops.key"))
    client = CommanderGrpcClient(grpc_stack.target)
    journal = DurableCommandRegistry(tmp_path / "commands.sqlite3")
    service = DeviceCommandService(
        client, _ReadsUnavailable(), InMemoryTrendStore(), journal
    )
    modbus_server.reset_write_count()
    try:
        yield service, modbus_server
    finally:
        await client.close()


def _decode_float32(registers: list[int]) -> float:
    """按 big-endian float32 解码两个 16 位寄存器。"""
    return struct.unpack(">f", struct.pack(">HH", registers[0], registers[1]))[0]


class TestWriteIdempotency:
    async def test_concurrent_same_command_id_executes_once(
        self, idem_env: tuple[DeviceCommandService, ModbusMockServer]
    ) -> None:
        service, server = idem_env
        command_id = "cmd-system-idem-concurrent"

        results = await asyncio.gather(
            *(
                service.send(
                    _DEVICE_ID, _POINT_ID, 66.6, timeout=10.0, command_id=command_id
                )
                for _ in range(8)
            )
        )

        assert all(r.outcome is WriteOutcome.CONFIRMED_SUCCESS for r in results)
        assert all(r.command_id == command_id for r in results)
        # 8 个并发同 id 请求共享一次执行——从站只收到一个写请求。
        assert server.write_count == 1
        registers = await server.read_holding(1, _SETPOINT_ADDRESS, 2)
        assert _decode_float32(registers) == pytest.approx(66.6, abs=0.1)

    async def test_repeated_command_id_returns_cached_result(
        self, idem_env: tuple[DeviceCommandService, ModbusMockServer]
    ) -> None:
        service, server = idem_env

        first = await service.send(
            _DEVICE_ID, _POINT_ID, 77.7, timeout=10.0, command_id="cmd-system-idem-cached"
        )
        assert first.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert server.write_count == 1

        # 完成后的同 id 重复请求命中持久化缓存——不再触发设备写。
        repeat = await service.send(
            _DEVICE_ID, _POINT_ID, 77.7, timeout=10.0, command_id="cmd-system-idem-cached"
        )
        assert repeat.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert repeat.command_id == "cmd-system-idem-cached"
        assert server.write_count == 1

        # 不同 command_id 是新的命令——必须真正下发。
        other = await service.send(
            _DEVICE_ID, _POINT_ID, 88.8, timeout=10.0, command_id="cmd-system-idem-other"
        )
        assert other.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert server.write_count == 2
        registers = await server.read_holding(1, _SETPOINT_ADDRESS, 2)
        assert _decode_float32(registers) == pytest.approx(88.8, abs=0.1)

    async def test_missing_command_id_gets_generated_id(
        self, idem_env: tuple[DeviceCommandService, ModbusMockServer]
    ) -> None:
        """调用方不带 command_id 时 Web 层生成 UUID 幂等标识并回填响应。"""
        service, server = idem_env

        result = await service.send(_DEVICE_ID, _POINT_ID, 55.5, timeout=10.0)

        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert result.command_id
        assert server.write_count == 1

    async def test_same_command_id_with_different_value_is_rejected(
        self, idem_env: tuple[DeviceCommandService, ModbusMockServer]
    ) -> None:
        service, server = idem_env
        command_id = "cmd-system-idem-conflict"
        first = await service.send(
            _DEVICE_ID, _POINT_ID, 44.4, timeout=10.0, command_id=command_id
        )
        assert first.outcome is WriteOutcome.CONFIRMED_SUCCESS

        with pytest.raises(CommandIdConflictError):
            await service.send(
                _DEVICE_ID, _POINT_ID, 99.9, timeout=10.0, command_id=command_id
            )
        assert server.write_count == 1
        registers = await server.read_holding(1, _SETPOINT_ADDRESS, 2)
        assert _decode_float32(registers) == pytest.approx(44.4, abs=0.1)

    async def test_pending_journal_record_blocks_retransmission(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        pki: Pki,
        grpc_stack: GrpcStack,
        modbus_server: ModbusMockServer,
    ) -> None:
        """崩溃现场（journal 预留停留 pending）：新实例同 ID 只给 UNKNOWN，
        绝不向真实 Commander 重新提交现场控制指令。"""
        monkeypatch.setenv(_CA_ENV, str(pki.tmp / "ca.crt"))
        monkeypatch.setenv(_CERT_ENV, str(pki.tmp / "ops.crt"))
        monkeypatch.setenv(_KEY_ENV, str(pki.tmp / "ops.key"))
        journal = DurableCommandRegistry(tmp_path / "crash.sqlite3")
        fingerprint = DeviceCommandService._fingerprint(_DEVICE_ID, _POINT_ID, 33.3, 10.0)
        state, _ = await journal.reserve("cmd-system-crash", fingerprint)
        assert state == "new"

        client = CommanderGrpcClient(grpc_stack.target)
        try:
            rebuilt = DeviceCommandService(
                client, _ReadsUnavailable(), InMemoryTrendStore(), journal
            )
            result = await rebuilt.send(
                _DEVICE_ID, _POINT_ID, 33.3, timeout=10.0, command_id="cmd-system-crash"
            )
        finally:
            await client.close()

        assert result.outcome is WriteOutcome.UNKNOWN
        assert "not retransmitted" in (result.error or "")
        assert modbus_server.write_count == 0

    async def test_successful_write_does_not_depend_on_readback_path(
        self, idem_env: tuple[DeviceCommandService, ModbusMockServer]
    ) -> None:
        """读回端口不可用时写入确认仍成立（readback_error 记录，不推翻成功）。"""
        service, _ = idem_env

        result = await service.send(
            _DEVICE_ID,
            _POINT_ID,
            12.5,
            timeout=10.0,
            command_id=f"cmd-no-readback-{uuid4().hex[:8]}",
        )

        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert result.success is True
        assert result.readback is None
        assert "no collector" in (result.readback_error or "")
