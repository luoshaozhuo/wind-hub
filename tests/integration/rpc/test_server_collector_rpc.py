"""Integration：Server 出站 gRPC 适配器 ↔ 真实 Collector 进程。

与 contract/rpc（proto 消息形状校验）不同，这里起真实 Collector
subprocess，用 Server 生产路径的 ``CollectorGrpcClient`` 完成 round trip：
状态查询 → Prepare/Activate/Abort → placement 快照 → 受栅栏保护的
Start/Stop。placement 拒绝路径（未下发快照直接 Start）必须映射为
``CollectorPlacementRejectedError``——Server 的 placement 安全语义依赖它。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.functional_config import update_yaml, write_functional_config
from tests.support.process import CollectorProcess
from wind_hub_core.config.fingerprint import fingerprint_config_set
from wind_hub_server.adapter.outbound.grpc.collector import CollectorGrpcClient
from wind_hub_server.application.port.worker import CollectorPlacementRejectedError

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"
WORKER_ID = "it-rpc"


@pytest.fixture
async def client(
    modbus_server,
    collector_factory,
    tmp_path: Path,
) -> tuple[CollectorGrpcClient, Path, CollectorProcess]:
    sink_path = tmp_path / "out" / "telemetry.jsonl"
    config_dir = write_functional_config(
        tmp_path / "cfg",
        modbus_server.port,
        sinks=[
            {
                "name": "file_sink",
                "type": "file",
                "connection": {
                    "path": str(sink_path),
                    "buffer_size": 4,
                    "flush_interval": 0.5,
                },
            }
        ],
        tasks=[
            {
                "task_id": TASK_ID,
                "device": "modbus-1",
                "point_group": "telemetry",
                "interval": 0.2,
                "targets": [{"sink": "file_sink"}],
            }
        ],
    )
    proc: CollectorProcess = await collector_factory(config_dir, collector_id=WORKER_ID)
    client = CollectorGrpcClient(proc.grpc_target)
    try:
        yield client, config_dir, proc
    finally:
        await client.close()


class TestQueryRoundTrip:
    async def test_config_status_reports_identity_and_hashes(self, client) -> None:
        grpc_client, _, _ = client
        status = await grpc_client.config_status()

        assert status.collector_id == WORKER_ID
        assert status.runtime_running is True
        assert status.config_hash
        assert status.active_config_hash

    async def test_runtime_status_and_metrics(self, client) -> None:
        grpc_client, _, _ = client
        runtime = await grpc_client.runtime_status()
        assert runtime.running is True

        metrics = await grpc_client.metrics_snapshot()
        assert metrics.counters is not None

    async def test_inventory_queries(self, client) -> None:
        grpc_client, _, _ = client
        devices = await grpc_client.list_devices()
        assert [d.device_id for d in devices] == ["modbus-1"]

        tasks = await grpc_client.list_tasks()
        assert [t.task_id for t in tasks] == [TASK_ID]

        instances = await grpc_client.list_task_instances()
        assert [i.instance_id for i in instances] == [INSTANCE_ID]


class TestConfigTransactionRoundTrip:
    async def test_prepare_activate_abort_cycle(self, client) -> None:
        grpc_client, config_dir, _ = client
        update_yaml(
            config_dir,
            "tasks.yaml",
            lambda data: data["tasks"][0].update({"interval": 0.5}),
        )
        config_hash = fingerprint_config_set(config_dir)

        prepared = await grpc_client.prepare_config("rev-it-1", config_hash)
        assert prepared.success is True, prepared.errors
        assert prepared.config_hash == config_hash

        status = await grpc_client.config_status()
        assert status.prepared_revision == "rev-it-1"

        activated = await grpc_client.activate_config("rev-it-1")
        assert activated.success is True, activated.errors
        assert activated.active_config_hash == config_hash

        status = await grpc_client.config_status()
        assert status.active_revision == "rev-it-1"
        assert status.active_config_hash == config_hash

    async def test_abort_discards_prepared_revision(self, client) -> None:
        grpc_client, config_dir, _ = client
        update_yaml(
            config_dir,
            "tasks.yaml",
            lambda data: data["tasks"][0].update({"interval": 0.5}),
        )
        config_hash = fingerprint_config_set(config_dir)

        prepared = await grpc_client.prepare_config("rev-it-abort", config_hash)
        assert prepared.success is True, prepared.errors

        aborted = await grpc_client.abort_config("rev-it-abort")
        assert aborted.success is True
        assert aborted.aborted is True

        # Abort 后同 revision 不能再 Activate。
        activated = await grpc_client.activate_config("rev-it-abort")
        assert activated.success is False

    async def test_prepare_rejects_hash_mismatch(self, client) -> None:
        grpc_client, _, _ = client
        prepared = await grpc_client.prepare_config("rev-it-bad", "0" * 64)
        assert prepared.success is False
        assert prepared.errors


class TestPlacementGateRoundTrip:
    async def test_start_without_placement_is_rejected(self, client) -> None:
        """未下发 placement 快照直接 Start：映射为端口层拒绝语义。"""
        grpc_client, _, _ = client
        with pytest.raises(CollectorPlacementRejectedError):
            await grpc_client.start_task_instance(INSTANCE_ID, 1)

    async def test_apply_placement_then_start_stop_cycle(self, client) -> None:
        grpc_client, _, _ = client
        placement = await grpc_client.apply_task_placement(WORKER_ID, 1, [TASK_ID])
        assert placement.success is True
        assert placement.generation == 1
        assert placement.task_count == 1

        started = await grpc_client.start_task_instance(INSTANCE_ID, 1)
        assert started.state == "running"

        instances = await grpc_client.list_task_instances()
        assert instances[0].state == "running"

        stopped = await grpc_client.stop_task_instance(INSTANCE_ID)
        assert stopped.state == "stopped"

    async def test_start_with_stale_generation_is_rejected(self, client) -> None:
        """placement generation 前进后，旧 generation 的 Start 必须被拒绝。"""
        grpc_client, _, _ = client
        await grpc_client.apply_task_placement(WORKER_ID, 2, [TASK_ID])

        with pytest.raises(CollectorPlacementRejectedError):
            await grpc_client.start_task_instance(INSTANCE_ID, 1)

        started = await grpc_client.start_task(TASK_ID, 2)
        assert started.task_id == TASK_ID

    async def test_placement_for_wrong_worker_is_rejected(self, client) -> None:
        """worker_id 与 Collector 身份不一致的快照必须被拒绝。"""
        grpc_client, _, _ = client
        with pytest.raises(CollectorPlacementRejectedError):
            await grpc_client.apply_task_placement("someone-else", 1, [TASK_ID])
