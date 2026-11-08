"""Integration：Server 出站 gRPC 适配器 ↔ 真实 Collector 进程（v1 双服务）。

起真实 Collector subprocess，用 Server 生产路径的 ``CollectorGrpcClient``
完成 round trip：身份/状态/清单查询 → 快照事务（GetConfigSnapshot/
PushConfigSnapshot/ApplyConfigSnapshot + 乐观 revision fencing）→ 设备级
START/STOP（管理员令牌 + task.revision 栅栏）。placement generation 栅栏
已移除：未携带管理员令牌的 START 必须 fail-closed（PERMISSION_DENIED），
陈旧 revision 必须映射为 ``CollectorTaskConflictError``（裸链路上为
ABORTED）。
"""

from __future__ import annotations

from pathlib import Path

import grpc
import pytest
import yaml

from collector.application.config_models import snapshot_digest
from contracts import collector_pb2_grpc, common_pb2, common_pb2_grpc
from tests.support import control
from tests.support.process import CollectorProcess
from tests.support.v2_config import SYSTEM_ADMIN_TOKEN, write_v2_modbus_tree
from tests.support.wait import wait_until
from web_server.adapter.outbound.grpc.collector import CollectorGrpcClient

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

DEVICE_ID = "modbus-1"
WORKER_ID = "it-rpc"
ADMIN_METADATA = (("x-windhub-admin-token", SYSTEM_ADMIN_TOKEN),)


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


def _collect_files(tree: Path) -> dict[str, bytes]:
    """镜像生产 SnapshotRepository.collect_source 的 Collector -owned 文件集。

    树内其他服务文档（commander/gateway/示例）与 observability 模板不属于
    Collector 快照，rglob 全量计算会与代际 revision 不一致。
    """
    files: dict[str, bytes] = {}
    for relative in (
        "services/collector.yaml",
        "instances/site.yaml",
        "instances/devices.yaml",
        "templates/units.yaml",
        "templates/business_points.yaml",
    ):
        files[relative] = (tree / relative).read_bytes()
    for category in ("protocols", "device_models", "point_tables", "tasks"):
        folder = tree / "templates" / category
        if not folder.is_dir():
            continue
        for path in folder.rglob("*.yaml"):
            files[path.relative_to(tree).as_posix()] = path.read_bytes()
    return files


def _tree_digest(tree: Path) -> str:
    """按 v1 整盘快照口径（collect_source 文件集）计算期望 revision。"""
    return snapshot_digest(_collect_files(tree))


class TestQueryRoundTrip:
    async def test_config_status_reports_identity_and_revision(self, stack) -> None:
        grpc_client, _tree, proc = stack
        status = await grpc_client.config_status()

        assert status.collector_id == WORKER_ID
        assert status.runtime_running is True
        assert status.active_revision

        # v1 身份语义：process_role 固定 collector，service_id 即实例身份。
        info = await control.collector_info(proc.grpc_target)
        assert info.process_role == "collector"
        assert info.service_id == WORKER_ID
        assert await control.config_revision(proc.grpc_target) == status.active_revision

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
        # v1 revision 是整盘有效文件集的字节级摘要，不再是单文档 config_hash。
        assert revision == _tree_digest(tree)

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
        candidate_content = yaml.safe_dump(candidate).encode("utf-8")

        # 陈旧 revision 的快照 Push 必须被乐观锁拒绝（ABORTED），不得落盘。
        files = await control.disk_snapshot_files(proc.grpc_target)
        channel = grpc.aio.insecure_channel(proc.grpc_target)
        try:
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await common_pb2_grpc.CommonServiceStub(channel).PushConfigSnapshot(
                    common_pb2.PushConfigSnapshotRequest(
                        files=[
                            common_pb2.SnapshotFile(
                                relative_path=path,
                                content=data,
                                size_bytes=len(data),
                            )
                            for path, data in sorted(files.items())
                        ],
                        expected_revision="0" * 64,
                    ),
                    metadata=ADMIN_METADATA,
                    timeout=10,
                )
            assert error.value.code() == grpc.StatusCode.ABORTED
        finally:
            await channel.close()

        result = await control.replace_config(
            proc.grpc_target,
            yaml.safe_dump(candidate),
            expected_revision=revision,
        )
        assert result.success, [issue.message for issue in result.errors]
        expected_files = _collect_files(tree)
        expected_files["instances/devices.yaml"] = candidate_content
        assert result.stored_revision == snapshot_digest(expected_files)
        assert await control.config_revision(proc.grpc_target) == result.stored_revision

    async def test_config_requires_admin_token(self, stack) -> None:
        _, _, proc = stack
        channel = grpc.aio.insecure_channel(proc.grpc_target)
        try:
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await common_pb2_grpc.CommonServiceStub(channel).GetConfigSnapshot(
                    common_pb2.GetConfigSnapshotRequest(scope=common_pb2.SNAPSHOT_SCOPE_DISK),
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
                await collector_pb2_grpc.CollectorServiceStub(channel).StartTask(
                    common_pb2.OperationRequest(
                        id=DEVICE_ID,
                        expected_revision=revision,
                    ),
                    timeout=5,
                )
            assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED
        finally:
            await channel.close()

    async def test_start_stop_cycle_converges_and_persists(self, stack) -> None:
        grpc_client, _tree, proc = stack

        # START 被接受即返回；actual_state 向 RUNNING 的收敛由采集侧异步完成，
        # 与 uc07 系统测试的 start_task + wait_actual 模式一致。
        started = await grpc_client.start_task(DEVICE_ID)
        assert started.device == DEVICE_ID
        assert started.enabled is True

        async def _running() -> bool | None:
            states = await control.instance_states(proc.grpc_target)
            return states == {f"{DEVICE_ID}:{DEVICE_ID}": "RUNNING"} or None

        await wait_until(_running, timeout=10, description="device RUNNING after START")

        # 意图已持久化进已提交快照的 devices.yaml 覆盖（v1 代际在 state-dir，
        # 不再回写源配置树）。
        committed = await control.disk_snapshot_files(proc.grpc_target)
        on_disk = yaml.safe_load(committed["instances/devices.yaml"])
        assert on_disk["devices"][DEVICE_ID]["overrides"]["task"]["desired_state"] == "START"

        stopped = await grpc_client.stop_task(DEVICE_ID)
        assert stopped.enabled is False

        async def _stopped() -> bool | None:
            states = await control.instance_states(proc.grpc_target)
            return states == {f"{DEVICE_ID}:{DEVICE_ID}": "STOPPED"} or None

        await wait_until(_stopped, timeout=10, description="device STOPPED after STOP")
        committed = await control.disk_snapshot_files(proc.grpc_target)
        on_disk = yaml.safe_load(committed["instances/devices.yaml"])
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
                await collector_pb2_grpc.CollectorServiceStub(channel).StartTask(
                    common_pb2.OperationRequest(
                        id=DEVICE_ID,
                        expected_revision="0" * 64,
                    ),
                    metadata=ADMIN_METADATA,
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
