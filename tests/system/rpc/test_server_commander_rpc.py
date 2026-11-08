"""Integration：Web 生产出站适配器 CommanderGrpcClient ↔ 真实 mTLS Commander。

N-03 后 Commander 只开 mTLS 安全端口（service.yaml ``security`` 节驱动，
``require_client_auth=True``），旧的明文子进程 harness 已无法连通。本文件
复用 ``tests/commander/grpc_harness.py`` 的真实 PKI（EC P-256 CA +
viewer/ops/deployer/stranger/rogue 证书）、``build_commander`` 组合根、
仅安全端口 gRPC 服务器与真实 Modbus TCP 从站，通过 **Web 生产适配器**
（三环境变量装载证书、无明文回退）走完正式 v1 合同：

- mTLS 边界：缺证拒构造、陌生身份 PERMISSION_DENIED、非信任 CA 握手失败；
- 查询聚合：GetServiceStatus + runtime 快照 + ListDevices 分页；
- 诊断：GetDevice + Diagnose 真实设备/TCP/协议三阶段；
- 控制：GetDevice 取 revision → SubmitCommand → 轮询 GetCommandResult，
  以真实 Modbus 从站寄存器回读为物理证据；
- 快照事务：Get/Push/ApplyConfigSnapshot + disk 回查确认 + 乐观 revision
  fencing + scope 鉴权。

全程真实 gRPC/TLS 链路与真实 Commander 服务，不使用 Mock。
"""

from __future__ import annotations

import struct
from pathlib import Path
from uuid import uuid4

import grpc
import pytest

from core.application.protocol_contract import WriteOutcome
from tests.commander.grpc_harness import GrpcStack, Pki, build_pki, build_stack
from tests.fixtures.servers.modbus_server import ModbusMockServer
from web_server.adapter.outbound.grpc.commander import CommanderGrpcClient
from web_server.application.device.command_model import Command

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

_DEVICE_ID = "WT01"
_SETPOINT_ADDRESS = 200
_CA_ENV = "WIND_HUB_COMMANDER_CA_CERT"
_CERT_ENV = "WIND_HUB_COMMANDER_CLIENT_CERT"
_KEY_ENV = "WIND_HUB_COMMANDER_CLIENT_KEY"


# ---------------------------------------------------------------------------
# 真实 mTLS 栈 fixture（与 tests/commander/integration 同一 harness）
# ---------------------------------------------------------------------------


@pytest.fixture
def pki(tmp_path: Path) -> Pki:
    # build_pki 直接在给定目录落证书文件，不自行建目录。
    return build_pki(tmp_path)


@pytest.fixture
async def grpc_stack(
    tmp_path: Path, pki: Pki, modbus_server: ModbusMockServer
) -> GrpcStack:
    stack, server, service = await build_stack(
        tmp_path,
        pki,
        modbus_port=modbus_server.port,
        label="system-rpc",
        stream_prefix="CMD_SYS",
    )
    try:
        yield stack
    finally:
        await server.stop(grace=5)
        await service.stop()


def _client(
    monkeypatch: pytest.MonkeyPatch,
    pki: Pki,
    stack: GrpcStack,
    *,
    identity: str,
    ca: str | None = None,
) -> CommanderGrpcClient:
    """以三环境变量装载真实证书构造 Web 生产适配器（无明文回退路径）。"""
    monkeypatch.setenv(_CA_ENV, str(pki.tmp / (ca or "ca.crt")))
    monkeypatch.setenv(_CERT_ENV, str(pki.tmp / f"{identity}.crt"))
    monkeypatch.setenv(_KEY_ENV, str(pki.tmp / f"{identity}.key"))
    return CommanderGrpcClient(stack.target)


def _decode_float32(words: list[int]) -> float:
    return struct.unpack(">f", struct.pack(">HH", words[0], words[1]))[0]


# ---------------------------------------------------------------------------
# mTLS 安全边界（适配器真实接线，不允许明文回退）
# ---------------------------------------------------------------------------


class TestMtlsClientBoundary:
    async def test_missing_cert_env_refuses_construction(
        self, monkeypatch: pytest.MonkeyPatch, pki: Pki, grpc_stack: GrpcStack
    ) -> None:
        for name in (_CA_ENV, _CERT_ENV, _KEY_ENV):
            monkeypatch.delenv(name, raising=False)

        with pytest.raises(RuntimeError, match="mTLS requires"):
            CommanderGrpcClient(grpc_stack.target)

    async def test_stranger_identity_is_permission_denied(
        self, monkeypatch: pytest.MonkeyPatch, pki: Pki, grpc_stack: GrpcStack
    ) -> None:
        client = _client(monkeypatch, pki, grpc_stack, identity="stranger")
        try:
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await client.status()
            assert exc_info.value.code() is grpc.StatusCode.PERMISSION_DENIED
        finally:
            await client.close()

    async def test_untrusted_ca_fails_at_tls_handshake(
        self, monkeypatch: pytest.MonkeyPatch, pki: Pki, grpc_stack: GrpcStack
    ) -> None:
        # rogue 证书由外部 CA 签发：服务器只信任 WindHub Test CA。
        client = _client(
            monkeypatch, pki, grpc_stack, identity="rogue", ca="rogue-ca.crt"
        )
        try:
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await client.status()
            assert exc_info.value.code() is grpc.StatusCode.UNAVAILABLE
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# 查询与诊断 round trip（ops：control + query）
# ---------------------------------------------------------------------------


class TestQueryRoundTrip:
    async def test_status_aggregates_health_devices_and_revision(
        self, monkeypatch: pytest.MonkeyPatch, pki: Pki, grpc_stack: GrpcStack
    ) -> None:
        client = _client(monkeypatch, pki, grpc_stack, identity="ops")
        try:
            status = await client.status()
        finally:
            await client.close()

        assert status.running is True
        # 共享配置树自带基线实例，WT01 由 harness 追加，至少一台可见。
        assert status.device_count >= 1
        # CommonService 不承诺旧版 active_config_hash：适配器不得伪造非空值。
        assert status.active_config_hash == ""
        assert status.active_revision
        assert status.prepared_revision is None

    async def test_verify_device_diagnoses_real_modbus_link(
        self,
        monkeypatch: pytest.MonkeyPatch,
        pki: Pki,
        grpc_stack: GrpcStack,
        modbus_server: ModbusMockServer,
    ) -> None:
        client = _client(monkeypatch, pki, grpc_stack, identity="ops")
        try:
            # Diagnose 不主动发起控制连接，只检查已建立的协议会话；
            # 先做一次真实写入让 Modbus 驱动完成连接与 T1/T2/T3 落地。
            write = await client.write(
                Command(
                    command_id=uuid4().hex,
                    device_id=_DEVICE_ID,
                    point_id="setpoint_power",
                    value=42.5,
                    timeout=10.0,
                )
            )
            assert write.outcome is WriteOutcome.CONFIRMED_SUCCESS, write.error
            assert modbus_server.write_count >= 1

            result = await client.verify_device(_DEVICE_ID, timeout=3.0)
        finally:
            await client.close()

        assert result.device_id == _DEVICE_ID
        assert result.protocol == "modbus"
        assert result.ok is True
        assert result.stages, "diagnostic stages must not be empty"
        assert all(stage.ok for stage in result.stages)


# ---------------------------------------------------------------------------
# 控制 round trip：正式 SubmitCommand/GetCommandResult + 真实从站物理证据
# ---------------------------------------------------------------------------


class TestCommandRoundTrip:
    async def test_write_reaches_modbus_slave_and_confirms(
        self,
        monkeypatch: pytest.MonkeyPatch,
        pki: Pki,
        grpc_stack: GrpcStack,
        modbus_server: ModbusMockServer,
    ) -> None:
        client = _client(monkeypatch, pki, grpc_stack, identity="ops")
        before = modbus_server.write_count
        try:
            result = await client.write(
                Command(
                    command_id=uuid4().hex,
                    device_id=_DEVICE_ID,
                    point_id="setpoint_power",
                    value=1500.0,
                    timeout=10.0,
                )
            )
        finally:
            await client.close()

        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS, result.error
        assert modbus_server.write_count == before + 1
        # 物理证据：直接从真实 Modbus 从站回读保持寄存器 200（float32, 2 words）。
        words = await modbus_server.read_holding(1, _SETPOINT_ADDRESS, count=2)
        assert _decode_float32(words) == pytest.approx(1500.0, abs=0.1)

    async def test_unknown_device_write_is_confirmed_failure(
        self, monkeypatch: pytest.MonkeyPatch, pki: Pki, grpc_stack: GrpcStack
    ) -> None:
        """GetDevice 失败发生在 Submit 之前：可确定本次控制未执行。"""
        client = _client(monkeypatch, pki, grpc_stack, identity="ops")
        try:
            result = await client.write(
                Command(
                    command_id=uuid4().hex,
                    device_id="ghost-404",
                    point_id="setpoint_power",
                    value=1.0,
                    timeout=5.0,
                )
            )
        finally:
            await client.close()

        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
        assert result.error


# ---------------------------------------------------------------------------
# 配置快照事务 round trip（deployer：reload + query）
# ---------------------------------------------------------------------------


class TestConfigSnapshotRoundTrip:
    async def test_push_apply_and_disk_confirmation(
        self, monkeypatch: pytest.MonkeyPatch, pki: Pki, grpc_stack: GrpcStack
    ) -> None:
        client = _client(monkeypatch, pki, grpc_stack, identity="deployer")
        try:
            disk = await client.get_config_snapshot("disk")
            assert disk.revision
            devices_name = next(name for name in disk.files if name.endswith("devices.yaml"))
            # 仅追加 YAML 注释：语义不变、字节变化，产生真实新提交。
            mutated = disk.files[devices_name] + b"\n# system-rpc snapshot round trip\n"

            pushed = await client.push_config({devices_name: mutated}, disk.revision)
            assert pushed.valid is True
            assert pushed.candidate_id
            assert pushed.base_revision == disk.revision

            applied = await client.apply_config(pushed.candidate_id, disk.revision)
            assert applied.effective_revision
            assert applied.errors == []
            assert isinstance(applied.restart_required, bool)

            confirmed = await client.get_config_snapshot("disk")
            assert applied.file_commit == confirmed.revision
            assert confirmed.files[devices_name] == mutated
        finally:
            await client.close()

    async def test_stale_expected_revision_is_aborted(
        self, monkeypatch: pytest.MonkeyPatch, pki: Pki, grpc_stack: GrpcStack
    ) -> None:
        client = _client(monkeypatch, pki, grpc_stack, identity="deployer")
        try:
            disk = await client.get_config_snapshot("disk")
            devices_name = next(name for name in disk.files if name.endswith("devices.yaml"))
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await client.push_config(
                    {devices_name: disk.files[devices_name]},
                    "stale-revision-not-on-disk",
                )
            assert exc_info.value.code() is grpc.StatusCode.ABORTED
        finally:
            await client.close()

    async def test_query_only_identity_cannot_push_snapshot(
        self, monkeypatch: pytest.MonkeyPatch, pki: Pki, grpc_stack: GrpcStack
    ) -> None:
        client = _client(monkeypatch, pki, grpc_stack, identity="viewer")
        try:
            disk = await client.get_config_snapshot("disk")
            devices_name = next(name for name in disk.files if name.endswith("devices.yaml"))
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await client.push_config(
                    {devices_name: disk.files[devices_name]}, disk.revision
                )
            assert exc_info.value.code() is grpc.StatusCode.PERMISSION_DENIED
        finally:
            await client.close()
