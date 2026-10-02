"""Integration：Server 出站 gRPC 适配器 ↔ 真实 Commander 进程 + 真实 Modbus 从站。

链路：CommanderGrpcClient（Server 生产路径出站适配器）→ 真实 Commander
subprocess → 真实 Modbus TCP 从站。覆盖状态查询、即时读/写、设备诊断与
Prepare/Activate/Abort 配置事务 round trip。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import grpc
import pytest

from tests.component.collector.conftest import update_yaml, write_functional_config
from tests.support.process import CollectorProcess
from wind_hub_core.config.fingerprint import fingerprint_config_set
from wind_hub_core.model.command import Command
from wind_hub_server.adapter.outbound.grpc.commander import CommanderGrpcClient

pytestmark = pytest.mark.modbus


@pytest.fixture
async def client(
    modbus_server,
    commander_factory,
    tmp_path: Path,
) -> tuple[CommanderGrpcClient, Path, CollectorProcess]:
    config_dir = write_functional_config(tmp_path / "cfg", modbus_server.port)
    proc: CollectorProcess = await commander_factory(config_dir)
    client = CommanderGrpcClient(proc.grpc_target)
    try:
        yield client, config_dir, proc
    finally:
        await client.close()


class TestQueryRoundTrip:
    async def test_status_reports_config_hashes(self, client) -> None:
        grpc_client, _, _ = client
        status = await grpc_client.status()

        assert status["running"] is True
        assert status["device_count"] == 1
        assert status["active_config_hash"]

    async def test_verify_device_diagnoses_real_link(self, client) -> None:
        grpc_client, _, _ = client
        result = await grpc_client.verify_device("modbus-1", timeout=2.0)

        assert result["device_id"] == "modbus-1"
        assert result["ok"] is True
        assert result["stages"], "diagnostic stages must not be empty"
        assert all(stage["ok"] for stage in result["stages"])


class TestReadWriteRoundTrip:
    async def test_read_point_returns_real_value(self, client) -> None:
        grpc_client, _, _ = client
        value = await grpc_client.read_point("modbus-1", "rotor.speed")

        assert value.device_id == "modbus-1"
        assert value.point_id == "rotor.speed"
        assert value.value == pytest.approx(1200.5)

    async def test_write_point_reaches_slave(self, client, modbus_server) -> None:
        grpc_client, _, _ = client
        before = modbus_server.write_count

        result = await grpc_client.write(
            Command(
                command_id=uuid4().hex,
                device_id="modbus-1",
                point_id="setpoint.power",
                value=1500.0,
            )
        )

        assert result.success is True, result.error
        assert modbus_server.write_count == before + 1

        # 回读确认从站寄存器真实更新。
        readback = await grpc_client.read_point("modbus-1", "setpoint.power")
        assert readback.value == pytest.approx(1500.0)


class TestConfigTransactionRoundTrip:
    async def test_prepare_activate_abort_cycle(self, client) -> None:
        grpc_client, config_dir, _ = client
        update_yaml(
            config_dir,
            "points.yaml",
            lambda data: data["point_tables"]["modbus"]["points"].append(
                {
                    "point_id": "gen.reactive",
                    "point_groups": ["telemetry"],
                    "address": {"register_type": "holding", "address": 106},
                    "data_type": "float32",
                }
            ),
        )
        config_hash = fingerprint_config_set(config_dir)

        prepared = await grpc_client.prepare_config("rev-cmd-1", config_hash)
        assert prepared["success"] is True
        assert prepared["config_hash"] == config_hash

        activated = await grpc_client.activate_config("rev-cmd-1")
        assert activated["success"] is True
        assert activated["active_config_hash"] == config_hash

        status = await grpc_client.status()
        assert status["active_revision"] == "rev-cmd-1"

    async def test_abort_discards_prepared_revision(self, client) -> None:
        grpc_client, config_dir, _ = client
        update_yaml(
            config_dir,
            "points.yaml",
            lambda data: data["point_tables"]["modbus"]["points"].append(
                {
                    "point_id": "gen.reactive",
                    "point_groups": ["telemetry"],
                    "address": {"register_type": "holding", "address": 106},
                    "data_type": "float32",
                }
            ),
        )
        config_hash = fingerprint_config_set(config_dir)

        prepared = await grpc_client.prepare_config("rev-cmd-abort", config_hash)
        assert prepared["success"] is True

        aborted = await grpc_client.abort_config("rev-cmd-abort")
        assert aborted["success"] is True
        assert aborted["aborted"] is True

        # Abort 后同 revision 不能再 Activate——Commander 以 INVALID_ARGUMENT
        # RPC 错误拒绝（契约：prepared revision 不匹配是调用方错误）。
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            await grpc_client.activate_config("rev-cmd-abort")
        assert exc_info.value.code() == grpc.StatusCode.INVALID_ARGUMENT
