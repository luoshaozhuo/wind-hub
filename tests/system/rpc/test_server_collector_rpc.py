"""Integration：Server 出站 gRPC 适配器 ↔ 真实 Collector 进程（v2 三路径）。

起真实 Collector subprocess，用 Server 生产路径的 ``CollectorGrpcClient``
完成 round trip：身份/状态/清单查询 → Config 五 RPC（GetConfig/ValidateConfig/
ReplaceConfig + 乐观 revision）→ 设备级 START/STOP（管理员令牌 + revision
fencing）。v1 的 Prepare/Activate/Abort 与 placement generation 栅栏已移除：
未携带管理员令牌的 START 必须 fail-closed（PERMISSION_DENIED），陈旧
revision 必须映射为 ``CollectorTaskConflictError``。
"""

from __future__ import annotations

from pathlib import Path

import grpc
import pytest
import yaml

from contracts import collector_v2_pb2 as pb
from contracts import collector_v2_pb2_grpc as pb_grpc
from core.infrastructure.config.versioned_templates import config_hash
from tests.support import control
from tests.support.process import CollectorProcess
from tests.support.v2_config import SYSTEM_ADMIN_TOKEN, write_v2_modbus_tree
from tests.support.wait import wait_until
from web_server.adapter.outbound.grpc.collector import CollectorGrpcClient

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

DEVICE_ID = "modbus-1"
WORKER_ID = "it-rpc"


@pytest.fixture
async def stack(
    modbus_server,
    collector_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CollectorGrpcClient, Path, CollectorProcess]:
    tree = write_v2_modbus_tree(tmp_path / "v2cfg", modbus_server.port)
    proc: CollectorProcess = await collector_factory(tree, collector_id=WORKER_ID)
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", SYSTEM_ADMIN_TOKEN)
    client = CollectorGrpcClient(proc.grpc_target)
    try:
        yield client, tree, proc
    finally:
        await client.close()


def _devices_doc(tree: Path) -> dict:
    return yaml.safe_load((tree / "instances" / "devices.yaml").read_text(encoding="utf-8"))


class TestQueryRoundTrip:
    async def test_config_status_reports_identity_and_revision(self, stack) -> None:
        grpc_client, tree, _ = stack
        status = await grpc_client.config_status()

        assert status.collector_id == WORKER_ID
        assert status.runtime_running is True
        assert status.active_revision

        info = await control.collector_info(stack[2].grpc_target)
        assert info.collector_id == WORKER_ID
        assert info.active_revision == status.active_revision

    async def test_runtime_status_and_metrics(self, stack) -> None:
        grpc_client, _, _ = stack
        runtime = await grpc_client.runtime_status()
        assert runtime.running is True
        assert runtime.device_count == 1

        metrics = await grpc_client.metrics_snapshot()
        assert metrics.counters is not None

    async def test_inventory_queries(self, stack) -> None:
        grpc_client, _, _ = stack
        devices = await grpc_client.list_devices()
        assert [d.device_id for d in devices] == [DEVICE_ID]

        tasks = await grpc_client.list_tasks()
        assert [(t.task_id, t.device) for t in tasks] == [(DEVICE_ID, DEVICE_ID)]
        assert tasks[0].runtime_state == "stopped"

        instances = await grpc_client.list_task_instances()
        assert [i.instance_id for i in instances] == [DEVICE_ID]


class TestConfigRoundTrip:
    async def test_get_config_revision_matches_on_disk_hash(self, stack) -> None:
        _, tree, proc = stack
        revision = await control.config_revision(proc.grpc_target)
        assert revision == config_hash(_devices_doc(tree))

    async def test_validate_accepts_current_and_rejects_unknown_field(self, stack) -> None:
        _, tree, proc = stack
        revision = await control.config_revision(proc.grpc_target)
        doc = _devices_doc(tree)

        accepted = await control.validate_config(
            proc.grpc_target, yaml.safe_dump(doc), expected_revision=revision
        )
        assert accepted.valid, [issue.message for issue in accepted.errors]

        bad = dict(doc)
        bad["devices"] = {k: dict(v) for k, v in doc["devices"].items()}
        bad["devices"][DEVICE_ID]["bogus_field"] = 1
        rejected = await control.validate_config(
            proc.grpc_target, yaml.safe_dump(bad), expected_revision=revision
        )
        assert rejected.valid is False
        assert rejected.errors

    async def test_replace_moves_revision_and_stale_revision_is_aborted(self, stack) -> None:
        _, tree, proc = stack
        revision = await control.config_revision(proc.grpc_target)
        doc = _devices_doc(tree)
        candidate = dict(doc)
        candidate["devices"] = {k: dict(v) for k, v in doc["devices"].items()}
        candidate["devices"][DEVICE_ID]["endpoint"] = dict(
            candidate["devices"][DEVICE_ID]["endpoint"], port=1503
        )

        # 陈旧 revision 的替换必须被乐观锁拒绝。
        stale = pb.ReplaceConfigRequest(
            selector=pb.ConfigSelector(scope=pb.INSTANCE, name="devices"),
            expected_revision="0" * 64,
            request_id="it-replace-stale",
            yaml_content=yaml.safe_dump(candidate),
        )
        channel = grpc.aio.insecure_channel(proc.grpc_target)
        try:
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await pb_grpc.CollectorConfigServiceStub(channel).ReplaceConfig(
                    stale,
                    metadata=(("x-windhub-admin-token", SYSTEM_ADMIN_TOKEN),),
                    timeout=10,
                )
            assert error.value.code() == grpc.StatusCode.ABORTED
        finally:
            await channel.close()

        result = await control.replace_config(
            proc.grpc_target,
            yaml.safe_dump(candidate),
            expected_revision=revision,
            request_id="it-replace-1",
        )
        assert result.success, [issue.message for issue in result.errors]
        assert result.stored_revision == config_hash(candidate)
        assert await control.config_revision(proc.grpc_target) == result.stored_revision

    async def test_config_requires_admin_token(self, stack) -> None:
        _, _, proc = stack
        channel = grpc.aio.insecure_channel(proc.grpc_target)
        try:
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await pb_grpc.CollectorConfigServiceStub(channel).GetConfig(
                    pb.GetConfigRequest(
                        selector=pb.ConfigSelector(scope=pb.INSTANCE, name="devices")
                    ),
                    timeout=5,
                )
            assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
        finally:
            await channel.close()


class TestDeviceStartStopFencing:
    async def test_start_without_admin_token_is_denied(self, stack) -> None:
        _, _, proc = stack
        revision = await control.config_revision(proc.grpc_target)
        channel = grpc.aio.insecure_channel(proc.grpc_target)
        try:
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await pb_grpc.CollectorTaskServiceStub(channel).StartTask(
                    pb.TaskChangeRequest(
                        device_id=DEVICE_ID,
                        expected_revision=revision,
                        request_id="it-no-token",
                    ),
                    timeout=5,
                )
            assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
        finally:
            await channel.close()

    async def test_start_stop_cycle_converges_and_persists(self, stack) -> None:
        grpc_client, _, proc = stack

        started = await grpc_client.start_task(DEVICE_ID)
        assert started.device == DEVICE_ID
        assert started.runtime_state == "running"

        async def _running() -> bool | None:
            states = await control.instance_states(proc.grpc_target)
            return states == {f"{DEVICE_ID}:{DEVICE_ID}": "RUNNING"} or None

        await wait_until(_running, timeout=10, description="device RUNNING after START")

        # 意图已持久化到 devices.yaml 覆盖。
        on_disk = _devices_doc(stack[1])
        assert on_disk["devices"][DEVICE_ID]["overrides"]["task"]["desired_state"] == "START"

        stopped = await grpc_client.stop_task(DEVICE_ID)
        assert stopped.runtime_state == "stopped"
        on_disk = _devices_doc(stack[1])
        assert on_disk["devices"][DEVICE_ID]["overrides"]["task"]["desired_state"] == "STOP"

    async def test_stale_revision_start_is_aborted(self, stack) -> None:
        """真实链路上陈旧 revision 必须被拒绝（ABORTED）。

        ABORTED → CollectorTaskConflictError 的出站映射由
        tests/web_server/test_grpc_wire_conversion.py 在端口单测钉死。
        """
        _, _, proc = stack
        channel = grpc.aio.insecure_channel(proc.grpc_target)
        try:
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await pb_grpc.CollectorTaskServiceStub(channel).StartTask(
                    pb.TaskChangeRequest(
                        device_id=DEVICE_ID,
                        expected_revision="0" * 64,
                        request_id="it-stale-revision",
                    ),
                    metadata=(("x-windhub-admin-token", SYSTEM_ADMIN_TOKEN),),
                    timeout=5,
                )
            assert error.value.code() == grpc.StatusCode.ABORTED
        finally:
            await channel.close()

    async def test_unknown_device_is_not_found(self, stack) -> None:
        grpc_client, _, proc = stack
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await grpc_client.start_task("ghost-device")
        assert error.value.code() == grpc.StatusCode.NOT_FOUND
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await control.get_task(proc.grpc_target, "ghost-device")
        assert error.value.code() == grpc.StatusCode.NOT_FOUND
