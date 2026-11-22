"""Real Collector recovery: invalid selection isolation and restart-only activation.

Repository publication + Observer hot convergence is verified separately.
This suite exercises a real Collector process, real Modbus and Sink while
checking that local unpublished candidates cannot corrupt an active generation.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from collector.infrastructure.config.validation import resolve_source_paths, validate_collector_snapshot
from core.application.errors import ConfigError
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.control import disk_snapshot_files, start_task_instance
from tests.support.process import CollectorProcess, start_collector
from tests.support.recovery_helpers import (
    DEVICE_ID,
    recovery_collector_tree,
    wait_telemetry_value,
    write_modbus_redis_config,
)
from tests.support.wait import wait_grpc_ready

pytestmark = [pytest.mark.modbus, pytest.mark.redis, pytest.mark.real_service]

SERVICE_NAME = "service_collector-realtime_v1.1.0.yaml"
DEVICE_NAME = "device_system_v1.0.0.yaml"
TASK_NAME = "collector_tasks_system_v1.0.0.yaml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


class TestReloadFailureResilience:
    async def test_invalid_candidate_rejected_then_valid_change_converges(
        self,
        modbus_server: ModbusMockServer,
        collector_factory,
        sink_service_factory,
        redis_service: str,
        nats_service: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        flat_dir = write_modbus_redis_config(
            tmp_path / "cfg", modbus_server.port, redis_service, nats_url=nats_service
        )
        await sink_service_factory()
        root = recovery_collector_tree(flat_dir, tmp_path / "v2", nats_url=nats_service)
        service = root / "services" / SERVICE_NAME
        proc: CollectorProcess = await collector_factory(root)
        await start_task_instance(proc.grpc_target, instance_id=DEVICE_ID)
        first = await wait_telemetry_value(redis_service)
        original = await disk_snapshot_files(proc.grpc_target)

        # An unpublished candidate references a missing model. Validate its
        # complete selected closure without changing a running process.
        source_device = root / "instances" / DEVICE_NAME
        bad_device = root / "instances/device_system_v1.0.1.yaml"
        invalid = copy.deepcopy(_load(source_device))
        invalid["file_version"] = "1.0.1"
        invalid["devices"][DEVICE_ID]["model"] = {
            "id": "ghost-model",
            "version": "9.9.9",
        }
        _dump(bad_device, invalid)
        selected = _load(root / "deployments/deployment_collector-system_v1.0.0.yaml")
        selected["deployment_id"] = "collector-invalid"
        selected["instances"]["device"]["system"]["file_version"] = "1.0.1"
        bad_manifest = root / "deployments/deployment_collector-invalid_v1.0.0.yaml"
        _dump(bad_manifest, selected)
        with monkeypatch.context() as scope:
            scope.setenv("WIND_HUB_DEPLOYMENT_ID", "collector-invalid")
            scope.setenv("WIND_HUB_DEPLOYMENT_VERSION", "1.0.0")
            with pytest.raises(ConfigError):
                validate_collector_snapshot(resolve_source_paths(service))

        assert proc.is_running()
        assert await disk_snapshot_files(proc.grpc_target) == original
        continued = await wait_telemetry_value(
            redis_service, after=str(first["sample_time"])
        )

        # A valid immutable Task Instance version is selected by a NEW
        # Deployment version; the existing runtime remains on its old revision.
        task_file = root / "instances" / TASK_NAME
        tasks = _load(task_file)
        tasks["file_version"] = "1.0.1"
        for task in tasks["tasks"].values():
            task["interval_ms"] = 500
        _dump(root / "instances/collector_tasks_system_v1.0.1.yaml", tasks)
        good_manifest = _load(root / "deployments/deployment_collector-system_v1.0.0.yaml")
        good_manifest["file_version"] = "1.0.1"
        good_manifest["instances"]["collector_tasks"]["system"]["file_version"] = "1.0.1"
        _dump(root / "deployments/deployment_collector-system_v1.0.1.yaml", good_manifest)
        assert await disk_snapshot_files(proc.grpc_target) == original

        # The active process is not Repository-connected; only a supervised
        # restart is permitted to activate its newly selected immutable files.
        proc.terminate()
        fresh_dir = tmp_path / "selected-runtime"
        fresh_dir.mkdir()
        restarted = start_collector(
            root,
            log_dir=fresh_dir,
            env_extra={
                "WIND_HUB_DEPLOYMENT_ID": "collector-system",
                "WIND_HUB_DEPLOYMENT_VERSION": "1.0.1",
            },
        )
        try:
            await wait_grpc_ready(restarted.grpc_target)
            applied = await disk_snapshot_files(restarted.grpc_target)
            new_task = yaml.safe_load(
                applied["instances/collector_tasks_system_v1.0.1.yaml"]
            )
            assert all(task["interval_ms"] == 500 for task in new_task["tasks"].values())
            await start_task_instance(restarted.grpc_target, instance_id=DEVICE_ID)
            final = await wait_telemetry_value(
                redis_service, after=str(continued["sample_time"])
            )
            assert final["quality"] == "good"
        finally:
            if restarted.is_running():
                restarted.terminate()
            restarted.close_log()
