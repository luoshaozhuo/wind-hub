"""Eight-service real E2E harness on the active e2e-final deployment.

All business processes are launched and driven exclusively through the formal
CLI process manager; middleware (NATS/JetStream, Redis, Kafka, TDengine) is
provisioned by the isolated R5 Docker stack. No in-process service instance
stands in for the chain.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

from tests.fixtures.services import r5_stack

REPO_ROOT = Path(__file__).resolve().parents[4]
SRC = REPO_ROOT / "src"
E2E_ROOT = REPO_ROOT / "deploy" / "config_roots" / "e2e-final"
BOOTSTRAPS = REPO_ROOT / "deploy" / "e2e"

CONFIGURATION_PORT = 51060
SERVICES = (
    "gateway",
    "gateway-target",
    "commander",
    "data-generator",
    "sink",
    "storage",
    "collector-realtime",
    "collector-reliable",
)

_TOKEN_ENVS = (
    "WIND_HUB_CONFIG_CLI_ADMIN_TOKEN",
    "WIND_HUB_CONFIG_COLLECTOR_TOKEN",
    "WIND_HUB_CONFIG_SINK_TOKEN",
    "WIND_HUB_CONFIG_GATEWAY_TOKEN",
    "WIND_HUB_CONFIGURATION_TOKEN",
    "WIND_HUB_CONFIG_COMMANDER_TOKEN",
    "WIND_HUB_CONFIG_GENERATOR_TOKEN",
    "WIND_HUB_COLLECTOR_ADMIN_TOKEN",
    "WIND_HUB_SINK_CLI_TOKEN",
    "WIND_HUB_SINK_WEB_TOKEN",
    "WIND_HUB_STORAGE_OPERATOR_TOKEN",
    "WIND_HUB_STORAGE_CURSOR_SECRET",
    "WIND_HUB_E2E_COMMANDER_EMS_TOKEN",
    "WIND_HUB_E2E_COMMANDER_GATEWAY_TOKEN",
    "WIND_HUB_E2E_COMMANDER_MAINTENANCE_TOKEN",
    "WIND_HUB_E2E_GATEWAY_COMMANDER_TOKEN",
    "WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN",
    "WIND_HUB_E2E_GATEWAY_OPERATOR_TOKEN",
    "WIND_HUB_E2E_GATEWAY_TARGET_COMMANDER_TOKEN",
    "WIND_HUB_E2E_GATEWAY_TARGET_GENERATOR_TOKEN",
    "WIND_HUB_E2E_GATEWAY_TARGET_OPERATOR_TOKEN",
    "WIND_HUB_E2E_GATEWAY_TARGET_SINK_TOKEN",
    "WIND_HUB_E2E_GENERATOR_CLI_TOKEN",
    "WIND_HUB_E2E_GENERATOR_GATEWAY_TOKEN",
)

_FIXED_PORTS = (
    51060,
    52051,
    52052,
    52053,
    52054,
    52061,
    52073,
    52074,
    52110,
)


def _free_fixed_ports() -> None:
    """Kill stray listeners left by an interrupted previous E2E run."""
    for port in _FIXED_PORTS:
        subprocess.run(
            ["fuser", "-k", "-n", "tcp", str(port)],
            capture_output=True,
            check=False,
        )


pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.kafka,
    pytest.mark.nats,
    pytest.mark.jetstream,
    pytest.mark.slow,
]


def _rewrite_endpoints(root: Path, stack: r5_stack.R5Stack) -> None:
    """Point every active-root YAML at the isolated middleware stack."""
    redis_host, _, redis_port = stack.redis.rpartition(":")
    for path in root.rglob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        text = text.replace("nats://127.0.0.1:4222", stack.nats)
        text = text.replace("127.0.0.1:9092", stack.kafka)
        text = text.replace("127.0.0.1:6041", stack.tdengine_rest)
        if path.parent.name == "instances" and path.name.startswith("sinks_"):
            text = text.replace("port: 6379", f"port: {redis_port}")
        text = text.replace("/var/lib/wind-hub/e2e-final", str(root.parent / "state"))
        text = text.replace("/opt/wind-hub/src", str(root.parent))
        path.write_text(text, encoding="utf-8")


def _write_configuration_bootstrap(root: Path, stack: r5_stack.R5Stack) -> Path:
    document = yaml.safe_load(
        (BOOTSTRAPS / "configuration_bootstrap_e2e_v1.0.0.yaml").read_text(encoding="utf-8")
    )
    state = root / "state" / "configuration"
    state.mkdir(parents=True, exist_ok=True)
    (state / "cursor-secret").write_bytes(b"configuration-e2e-cursor-secret-32-bytes")
    (state / "cursor-secret").chmod(0o600)
    document["grpc"]["port"] = CONFIGURATION_PORT
    document["repository"]["sqlite_path"] = str(state / "configuration.db")
    document["repository"]["backup_root"] = str(state / "backups")
    document["nats"]["servers"] = [stack.nats]
    document["security"]["cursor_secret_file"] = str(state / "cursor-secret")
    path = root / "bootstraps" / "configuration.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _write_storage_bootstrap(root: Path, stack: r5_stack.R5Stack) -> Path:
    document = yaml.safe_load(
        (BOOTSTRAPS / "storage_bootstrap_e2e_v1.0.0.yaml").read_text(encoding="utf-8")
    )
    document["configuration"]["endpoint"] = f"127.0.0.1:{CONFIGURATION_PORT}"
    document["nats"]["servers"] = [stack.nats]
    cache = root / "state" / "storage"
    cache.mkdir(parents=True, exist_ok=True)
    document["cache"]["directory"] = str(cache)
    path = root / "bootstraps" / "storage.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _write_cli_config(root: Path, configuration_bootstrap: Path, storage_bootstrap: Path) -> Path:
    """Adapt the shipped CLI process map to this isolated root and interpreter."""
    document = yaml.safe_load(
        (E2E_ROOT / "services" / "service_cli-e2e_v1.1.8.yaml").read_text(encoding="utf-8")
    )
    services = document["services"]
    for name, entry in services.items():
        process = entry.get("process")
        if not process:
            continue
        argv = [str(part) for part in process.get("argv", ())]
        argv = [part.replace("/opt/wind-hub/src", str(root)) for part in argv]
        argv = [sys.executable if part == "/usr/bin/python3" else part for part in argv]
        argv = [part.replace("/var/lib/wind-hub/e2e-final", str(root / "state")) for part in argv]
        if name == "configuration":
            argv[-1] = str(configuration_bootstrap)
        if name == "storage":
            argv[-1] = str(storage_bootstrap)
        if name in {"collector-realtime", "collector-reliable"}:
            argv = [
                part.replace(
                    "service_collector-e2e-realtime_v1.0.0.yaml",
                    "service_collector-e2e-realtime_v2.0.0.yaml",
                ).replace(
                    "service_collector-e2e-reliable_v1.0.0.yaml",
                    "service_collector-e2e-reliable_v2.0.0.yaml",
                )
                for part in argv
            ]
        if name == "sink":
            argv = [
                part.replace("service_sink-e2e_v1.0.0.yaml", "service_sink-e2e_v2.0.0.yaml")
                for part in argv
            ]
            argv = [
                str(root) if part in {str(REPO_ROOT), "/opt/wind-hub/src"} else part
                for part in argv
            ]
        process["argv"] = argv
        process["cwd"] = str(REPO_ROOT)
        process["state_dir"] = str(root / "state" / "cli-processes")
        process["log_file"] = str(root / "logs" / f"{name}.log")
    document["service_id"] = "cli-e2e"
    path = root / "cli.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)
    return path


def _service_load_has_problem(harness: EightServiceHarness, name: str, endpoint: str) -> str:
    """Inspect a process log for an import/config failure marker."""
    log = harness.logs / f"{name}.log"
    if not log.exists():
        return f"{name}: log missing at {log}"
    tail = log.read_text(encoding="utf-8", errors="replace")[-2000:]
    return tail


@dataclass
class EightServiceHarness:
    root: Path
    stack: r5_stack.R5Stack
    cli_config: Path
    env: dict[str, str]
    logs: Path
    _started: list[str] = field(default_factory=list)

    def cli(
        self,
        *arguments: str,
        check: bool = True,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "cli.main",
                "--config",
                str(self.cli_config),
                "--format",
                "json",
                *arguments,
            ],
            cwd=SRC,
            env={**os.environ, **self.env, "PYTHONPATH": f"{SRC}:{REPO_ROOT}"},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
        payload = json.loads(lines[-1]) if lines else {}
        payload.setdefault("exit_code", completed.returncode)
        payload.setdefault("stderr", completed.stderr)
        if check:
            assert completed.returncode == 0, (
                arguments,
                completed.returncode,
                completed.stdout[-2000:],
                completed.stderr[-2000:],
            )
        return payload

    def start(self, name: str, *, check: bool = True) -> dict[str, Any]:
        # Record before invoking so a partial CLI start is always cleaned up.
        self._started.append(name)
        return self.cli("service", "start", name, "--yes", check=check, timeout=120)

    def stop(self, name: str) -> None:
        self.cli("service", "stop", name, "--yes", check=False, timeout=60)

    def status(self, name: str) -> dict[str, Any]:
        return self.cli("service", "status", name, check=False, timeout=60)

    def wait_healthy(self, name: str, *, timeout: float = 90.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.status(name)
            data = last.get("data") or []
            items = data if isinstance(data, list) else [data]
            business = (items[0].get("business") if items else {}) or {}
            if business.get("health") == "HEALTHY":
                return last
            time.sleep(0.5)
        raise AssertionError(
            f"{name} did not become HEALTHY: {last}; {_service_load_has_problem(self, name, name)}"
        )

    def wait_targets(self, name: str, *, timeout: float = 120.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.status(name)
            data = last.get("data") or []
            items = data if isinstance(data, list) else [data]
            business = (items[0].get("business") if items else {}) or {}
            if (
                business.get("health") == "HEALTHY"
                and business.get("target_revision")
                and business.get("target_revision") == business.get("active_revision")
            ):
                return last
            time.sleep(0.5)
        raise AssertionError(f"{name} did not activate its target revision: {last}")


@pytest.fixture(scope="module")
def stack() -> Iterator[r5_stack.R5Stack]:
    if not r5_stack.docker_available():
        pytest.skip("NOT_RUN: docker daemon unavailable")
    running = r5_stack.up_stack()
    try:
        yield running
    finally:
        r5_stack.down_stack(running)


async def _ensure_streams(server: str) -> None:
    import nats

    client = await nats.connect(server)
    try:
        js = client.jetstream()
        for name, subject, max_age in (
            ("WH_E2E_HISTORY", "wh.hist.batch.>", 3600),
            ("WH_E2E_DLQ", "wh.dlq", 2592000),
            ("COMMAND_AUDIT", "windhub.commander.audit", 3600),
        ):
            try:
                await js.add_stream(
                    name=name,
                    subjects=[subject],
                    storage="file",
                    discard="new",
                    max_bytes=1024 * 1024 * 1024,
                    max_age=max_age,
                )
            except Exception:  # noqa: BLE001 - existing stream is acceptable
                await js.stream_info(name)
    finally:
        await client.close()


async def _ensure_topic(bootstrap: str, topic: str) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        with contextlib.suppress(TopicAlreadyExistsError):
            await admin.create_topics(
                [NewTopic(name=topic, num_partitions=3, replication_factor=1)]
            )
    finally:
        await admin.close()


@pytest.fixture
def harness(tmp_path: Path, stack: r5_stack.R5Stack) -> Iterator[EightServiceHarness]:
    root = tmp_path / "e2e"
    root.mkdir()
    configs = root / "configs"
    for folder in ("deployments", "instances", "services", "templates"):
        shutil.copytree(E2E_ROOT / folder, configs / folder)
    _rewrite_endpoints(configs, stack)
    data_level = os.environ.get("EIGHT_SERVICE_SINK_DATA_LEVEL", "protocol")
    if data_level not in {"protocol", "raw"}:
        raise ValueError("EIGHT_SERVICE_SINK_DATA_LEVEL must be protocol or raw")
    history_template = (
        configs / "templates" / "deliveries" / "delivery_reliable_history_v1.1.0.yaml"
    )
    document = yaml.safe_load(history_template.read_text(encoding="utf-8"))
    document["output"]["data_level"] = data_level
    history_template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    configuration_bootstrap = _write_configuration_bootstrap(root, stack)
    storage_bootstrap = _write_storage_bootstrap(root, stack)
    cli_config = _write_cli_config(root, configuration_bootstrap, storage_bootstrap)

    env = {name: secrets.token_hex(24) for name in _TOKEN_ENVS}
    env["WIND_HUB_TDENGINE_USER"] = "root"
    env["WIND_HUB_TDENGINE_PASSWORD"] = "taosdata"
    env["WH_E2E_REDIS_PASSWORD"] = secrets.token_hex(16)
    env["WH_E2E_KAFKA_CREDENTIAL"] = "{}"
    env["WIND_HUB_GATEWAY_SINK_TOKEN"] = env["WIND_HUB_E2E_GATEWAY_TARGET_SINK_TOKEN"]

    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    _free_fixed_ports()
    result = EightServiceHarness(root, stack, cli_config, env, logs)
    try:
        yield result
    finally:
        for name in reversed(result._started):
            result.cli("service", "stop", name, "--yes", check=False, timeout=60)
        _free_fixed_ports()


@pytest.fixture
def started(harness: EightServiceHarness) -> EightServiceHarness:
    """Configuration first, publish the active root, then every business process."""
    harness.start("configuration")
    harness.wait_healthy("configuration")
    publish = harness.cli(
        "config",
        "publish",
        "--root",
        str(harness.root / "configs"),
        timeout=180,
    )
    assert publish["status"] == "SUCCESS", publish
    harness.env["WIND_HUB_CONFIGURATION_REVISION"] = str(int(publish["data"]["revision_id"]))
    asyncio.run(_ensure_topic(harness.stack.kafka, "windhub.e2e.history"))
    asyncio.run(_ensure_streams(harness.stack.nats))
    for name in SERVICES:
        # Collectors may report UNKNOWN briefly before their snapshot converges;
        # readiness is asserted below with a bounded wait instead of the CLI probe.
        payload = harness.start(
            name, check=name not in {"collector-realtime", "collector-reliable", "sink"}
        )
        if name not in {"collector-realtime", "collector-reliable", "sink"}:
            assert payload["status"] == "SUCCESS", payload
    for name in SERVICES:
        if name == "storage":
            harness.wait_targets(name)
        else:
            harness.wait_healthy(name)
    return harness
