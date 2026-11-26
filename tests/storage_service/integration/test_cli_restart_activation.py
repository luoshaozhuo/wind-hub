"""T-63: real CLI stop/start activates a staged static Storage configuration.

Both Configuration and Storage run as real independent processes. Configuration
is published through its gRPC API; Storage is started and stopped only through
the formal CLI process manager. Target/active revisions and the RESTART_REQUIRED
state are observed through real gRPC calls, never through in-process watcher
objects.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import grpc
import pytest
import yaml
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import configuration_repository_pb2 as pb
from tests.cli.e2e_util import last_json, run_cli
from tests.fixtures.services import r5_stack
from tests.support.configuration_live import (
    LiveConfiguration,
    reserve_port,
    write_live_configuration,
)

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.restart,
    pytest.mark.reload,
    pytest.mark.kafka,
]

_RELATIVE_DIRS = {
    "units": "templates",
    "business_points": "templates",
    "point_table": "templates/point_tables",
    "device_model": "templates/device_models",
    "site": "instances",
    "device": "instances",
    "service": "services",
    "deployment": "deployments",
}


def _input(
    kind: str, identity: str, version: str, document: dict[str, Any]
) -> pb.ConfigDocumentInput:
    file_name = f"{kind}_{identity}_v{version}.yaml"
    return pb.ConfigDocumentInput(
        config_id=pb.ConfigId(config_type=kind, config_id=identity),
        file_name=file_name,
        relative_path=f"{_RELATIVE_DIRS[kind]}/{file_name}",
        yaml_bytes=yaml.safe_dump(document, sort_keys=False).encode("utf-8"),
    )


def _base_documents() -> list[pb.ConfigDocumentInput]:
    return [
        _input(
            "units",
            "standard",
            "1.0.0",
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "quantities": {"power": {"base_unit": "kilowatt"}},
                "units": {"kilowatt": {"quantity": "power", "symbol": "kW"}},
            },
        ),
        _input(
            "business_points",
            "standard",
            "1.0.0",
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "business_points": {"power": {"data_type": "float32", "unit": "kilowatt"}},
            },
        ),
        _input(
            "point_table",
            "storage-points",
            "1.0.0",
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "template_id": "storage-points",
                "protocol": "modbus",
                "device_type": "turbine",
                "points": {
                    "power": {
                        "unit": "kilowatt",
                        "business_point_id": "power",
                        "data_type": "float32",
                    }
                },
            },
        ),
        _input(
            "device_model",
            "storage-model",
            "1.0.0",
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "template_id": "storage-model",
                "device_type_id": "turbine",
                "point_table": {"id": "storage-points", "version": "1.0.0"},
            },
        ),
        _input(
            "site",
            "example_site",
            "1.0.0",
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "site_id": "farm-a",
            },
        ),
        _input(
            "device",
            "farm-a",
            "1.0.0",
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "devices": {"WTG01": {"model": {"id": "storage-model", "version": "1.0.0"}}},
            },
        ),
    ]


def _documents(
    service_document: dict[str, Any], deployment_version: str
) -> list[pb.ConfigDocumentInput]:
    service_version = str(service_document["file_version"])
    return [
        *_base_documents(),
        _input("service", "storage-main", service_version, service_document),
        _input(
            "deployment",
            "prod-a",
            deployment_version,
            {
                "schema_version": 1,
                "file_version": deployment_version,
                "deployment_id": "prod-a",
                "services": {
                    "storage-main": {
                        "config_id": "storage-main",
                        "file_version": service_version,
                    }
                },
                "instances": {
                    "site": {"example_site": {"file_version": "1.0.0"}},
                    "device": {"farm-a": {"file_version": "1.0.0"}},
                },
            },
        ),
    ]


async def _publish(live: LiveConfiguration, documents: list[pb.ConfigDocumentInput]) -> int:
    repository, _, channel = live.stubs("deployer")
    try:
        current = await repository.GetCurrentRevision(pb.GetCurrentRevisionRequest(), timeout=10)
        call_id = uuid4().hex
        proposal = await repository.CreateCandidate(
            pb.CreateCandidateRequest(
                request_id=f"f63-create-{call_id}",
                expected_revision_id=current.revision_id,
                documents=documents,
            ),
            timeout=15,
        )
        assert proposal.valid, proposal.issues
        published = await repository.PublishCandidate(
            pb.PublishCandidateRequest(
                request_id=f"f63-publish-{call_id}",
                candidate_id=proposal.candidate_id,
                expected_revision_id=current.revision_id,
            ),
            timeout=15,
        )
        return int(published.revision_id)
    finally:
        await channel.close()


def _service_document(
    *, stack: Any, storage_port: int, token: str, work_root: Path
) -> dict[str, Any]:
    document = yaml.safe_load(
        (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
    )
    body = document["service"]
    body["kafka"].update(
        {
            "bootstrap_servers": stack.kafka,
            "topic": f"storage-f63-{token}",
            "group_id": f"storage-f63-{token}",
        }
    )
    body["grpc"]["listen_port"] = storage_port
    body["tdengine"]["endpoint"] = stack.tdengine_rest
    body["export"]["root"] = str(work_root / "exports")
    body["backup"]["root"] = str(work_root / "backups")
    body["configuration"]["reconcile_interval_s"] = 5
    return document


def _write_cli_yaml(
    path: Path, *, storage_port: int, bootstrap: Path, work_root: Path, env: dict[str, str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "cli": {
            "timeout_seconds": 30,
            "output_format": "json",
            "prompt": "f63> ",
            "poll_interval_ms": 200,
        },
        "services": {
            "storage": {
                "grpc_endpoint": f"127.0.0.1:{storage_port}",
                "manager": "process",
                "target": "storage",
                "process": {
                    "argv": [
                        sys.executable,
                        "-m",
                        "storage_service.main",
                        "--bootstrap",
                        str(bootstrap),
                    ],
                    "cwd": str(SRC),
                    "env": env,
                    "state_dir": str(work_root / "state"),
                    "log_file": str(work_root / "logs" / "storage.log"),
                },
            }
        },
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)


async def _service_status(channel: grpc.aio.Channel, token: str) -> common.GetServiceStatusResponse:
    stub = common_rpc.CommonServiceStub(channel)
    return await stub.GetServiceStatus(
        common.Empty(),
        timeout=10,
        metadata=(("authorization", f"Bearer {token}"),),
    )


async def _wait_status(
    channel: grpc.aio.Channel, token: str, predicate: Any, description: str
) -> common.GetServiceStatusResponse:
    deadline = time.monotonic() + 60
    last: common.GetServiceStatusResponse | None = None
    while time.monotonic() < deadline:
        try:
            last = await _service_status(channel, token)
        except grpc.aio.AioRpcError:
            await asyncio.sleep(0.5)
            continue
        if predicate(last):
            return last
        await asyncio.sleep(0.5)
    raise AssertionError(f"timed out waiting for {description}; last={last}")


def _snapshot(cache_root: Path, name: str) -> dict[str, Any]:
    envelope = json.loads((cache_root / "configuration-snapshots" / name).read_bytes())
    return envelope["payload"]


async def _create_storage_topic(bootstrap: str, topic: str) -> None:
    """Provision the target topic explicitly; broker auto-create is not a test precondition."""
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        with contextlib.suppress(TopicAlreadyExistsError):
            await admin.create_topics(
                [NewTopic(name=topic, num_partitions=1, replication_factor=1)]
            )
    finally:
        await admin.close()


@pytest.mark.asyncio
async def test_cli_restart_activates_staged_static_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not r5_stack.docker_available():
        pytest.skip("NOT_RUN: docker daemon unavailable")
    pytest.importorskip("taosws", reason="NOT_RUN: real TDengine SQL driver unavailable")
    stack = await asyncio.to_thread(r5_stack.up_stack)
    token = uuid4().hex[:10]
    storage_port = reserve_port()
    operator_token = f"f63-storage-operator-{token}-0123456789abcdef"
    cursor_secret = f"f63-storage-cursor-{token}-0123456789abcdef"
    # The CLI process attaches this bearer itself when polling gRPC health.
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", operator_token)
    live: LiveConfiguration | None = None
    cli_yaml = tmp_path / "cli" / "cli.yaml"
    cache_root = tmp_path / "storage-cache"
    bootstrap = tmp_path / "storage-bootstrap.yaml"
    try:
        # --- Real Configuration process, launched through its formal CLI. ---
        root = tmp_path / "configuration"
        root.mkdir(parents=True)
        live = write_live_configuration(root, reserve_port())
        # The mainline live harness provisions the viewer token itself.
        viewer_token = live.tokens["viewer"]
        started = await asyncio.to_thread(
            live.cli, "service", "start", "configuration", "--yes", check=False
        )
        assert started["exit_code"] == 0, started

        # --- Revision A: baseline closure. ---
        service_a = _service_document(
            stack=stack, storage_port=storage_port, token=token, work_root=tmp_path
        )
        revision_a = await _publish(live, _documents(service_a, "1.0.0"))
        assert revision_a >= 1
        await _create_storage_topic(stack.kafka, service_a["service"]["kafka"]["topic"])

        bootstrap.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "file_version": "1.0.0",
                    "service_id": "storage-main",
                    "deployment_id": "prod-a",
                    "configuration": {
                        "endpoint": f"127.0.0.1:{live.port}",
                        "token_env": "WIND_HUB_CONFIG_VIEWER_TOKEN",
                    },
                    "nats": {"servers": [stack.nats]},
                    "cache": {"directory": str(cache_root)},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        process_env = {
            "WIND_HUB_CONFIG_VIEWER_TOKEN": viewer_token,
            "WIND_HUB_STORAGE_OPERATOR_TOKEN": operator_token,
            "WIND_HUB_TDENGINE_USER": "root",
            "WIND_HUB_TDENGINE_PASSWORD": "taosdata",
            "WIND_HUB_STORAGE_CURSOR_SECRET": cursor_secret,
            "WIND_HUB_TEST_NATS_URL": stack.nats,
        }
        _write_cli_yaml(
            cli_yaml,
            storage_port=storage_port,
            bootstrap=bootstrap,
            work_root=tmp_path,
            env=process_env,
        )

        # --- Start Storage through the formal CLI process manager. ---
        start = await asyncio.to_thread(run_cli, cli_yaml, "service", "start", "storage", "--yes")
        start_payload = last_json(start.stdout)
        assert start.returncode == 0, start.stderr
        assert start_payload["status"] == "SUCCESS", start_payload
        assert start_payload["data"]["process"]["confirmed"] is True
        assert start_payload["data"]["business"]["health"] == "HEALTHY"

        channel = grpc.aio.insecure_channel(f"127.0.0.1:{storage_port}")
        try:
            ready = await _wait_status(
                channel,
                operator_token,
                lambda s: s.health == "HEALTHY"
                and s.target_revision == s.active_revision
                and s.dependencies[0].detail == "READY",
                "READY with matching target/active revisions",
            )
            target_a = ready.target_revision
            assert target_a and target_a == ready.active_revision

            accepted_path = cache_root / "configuration-snapshots" / "accepted_bundle.json"
            pending_path = cache_root / "configuration-snapshots" / "target_bundle.json"
            assert accepted_path.exists()
            assert not pending_path.exists()
            accepted_a = _snapshot(cache_root, "accepted_bundle.json")
            assert accepted_a["revision_id"] == revision_a
            assert accepted_a["config"]["query"]["max_page_size"] == 1000
            # --- Revision B: static parameter change (query page size 999). ---
            service_b = json.loads(json.dumps(service_a))
            service_b["file_version"] = "2.0.1"
            service_b["service"]["query"]["max_page_size"] = 999
            revision_b = await _publish(live, _documents(service_b, "1.0.1"))
            assert revision_b > revision_a

            restart_state = await _wait_status(
                channel,
                operator_token,
                lambda s: s.target_revision != s.active_revision
                and s.dependencies[0].detail == "RESTART_REQUIRED",
                "RESTART_REQUIRED with split target/active revisions",
            )
            target_b = restart_state.target_revision
            assert target_b != target_a
            assert restart_state.active_revision == target_a
            assert restart_state.health == "DEGRADED"

            # The pending target is durable while the accepted closure stays at A.
            assert pending_path.exists()
            pending = _snapshot(cache_root, "target_bundle.json")
            assert pending["revision_id"] == revision_b
            assert pending["target_revision"] == target_b
            assert pending["config"]["query"]["max_page_size"] == 999
            accepted_still_a = _snapshot(cache_root, "accepted_bundle.json")
            assert accepted_still_a["target_revision"] == target_a
            assert accepted_still_a["config"]["query"]["max_page_size"] == 1000

            # --- CLI stop and restart; the process must activate B itself. ---
            stop = await asyncio.to_thread(run_cli, cli_yaml, "service", "stop", "storage", "--yes")
            assert stop.returncode == 0, stop.stderr
            assert last_json(stop.stdout)["status"] == "SUCCESS"
            restart = await asyncio.to_thread(
                run_cli, cli_yaml, "service", "start", "storage", "--yes"
            )
            restart_payload = last_json(restart.stdout)
            assert restart.returncode == 0, restart.stderr
            assert restart_payload["status"] == "SUCCESS", restart_payload

            activated = await _wait_status(
                channel,
                operator_token,
                lambda s: s.health == "HEALTHY"
                and s.target_revision == s.active_revision == target_b
                and s.dependencies[0].detail == "READY",
                "B activated after CLI restart",
            )
            assert activated.target_revision == target_b
            assert activated.active_revision == target_b
            accepted_b = _snapshot(cache_root, "accepted_bundle.json")
            assert accepted_b["revision_id"] == revision_b
            assert accepted_b["target_revision"] == target_b
            assert accepted_b["config"]["query"]["max_page_size"] == 999
            # Historical PointTable and Device Instance bindings are preserved.
            assert "point_table_storage-points_v1.0.0" in accepted_b["point_table_versions"]
            bindings = accepted_b["device_bindings"]
            assert any(item["device_id"] == "WTG01" for item in bindings)
            assert (
                not pending_path.exists()
                or _snapshot(cache_root, "target_bundle.json")["target_revision"] == target_b
            )
        finally:
            await channel.close()
    finally:
        stop = await asyncio.to_thread(run_cli, cli_yaml, "service", "stop", "storage", "--yes")
        if cli_yaml.exists():
            assert stop.returncode == 0, (stop.stdout, stop.stderr)
        if live is not None:
            stopped = await asyncio.to_thread(
                live.cli, "service", "stop", "configuration", "--yes", check=False
            )
            assert stopped["exit_code"] == 0, stopped
        await asyncio.to_thread(r5_stack.down_stack, stack)
