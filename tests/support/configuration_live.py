"""Real Configuration Service process and Bearer CLI integration harness."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import yaml

from contracts import common_pb2_grpc, configuration_repository_pb2_grpc
from tests.commander.grpc_harness import Pki
from tests.support.bearer import bearer_channel, ensure_bearer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


@dataclass
class LiveConfiguration:
    root: Path
    pki: Pki
    port: int
    cli_path: Path
    bootstrap_path: Path
    database_path: Path

    def channel(self, identity: str = "deployer", *, ca: str = "ca") -> grpc.aio.Channel:
        return bearer_channel(f"127.0.0.1:{self.port}", identity)

    def stubs(
        self, identity: str = "deployer", *, ca: str = "ca"
    ) -> tuple[
        configuration_repository_pb2_grpc.ConfigurationRepositoryServiceStub,
        common_pb2_grpc.CommonServiceStub,
        grpc.aio.Channel,
    ]:
        channel = self.channel(identity, ca=ca)
        return (
            configuration_repository_pb2_grpc.ConfigurationRepositoryServiceStub(channel),
            common_pb2_grpc.CommonServiceStub(channel),
            channel,
        )

    def cli(
        self,
        *arguments: str,
        check: bool = True,
        config: Path | None = None,
        identity: str = "deployer",
    ) -> dict[str, Any]:
        token_ref = ensure_bearer(identity)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "cli.main",
                "--config",
                str(config or self.cli_path),
                "--format",
                "json",
                *arguments,
            ],
            cwd=SRC,
            env={
                **os.environ,
                "PYTHONPATH": str(SRC),
                "WIND_HUB_CONFIG_CLI_TOKEN": os.environ[token_ref.removeprefix("env:")],
            },
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
        payload = json.loads(lines[-1]) if lines else {}
        if check:
            assert completed.returncode == 0, (
                arguments,
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
        payload["exit_code"] = completed.returncode
        payload["stderr"] = completed.stderr
        return payload


def reserve_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_live_configuration(root: Path, pki: Pki, port: int) -> LiveConfiguration:
    database = root / "state/configuration.sqlite3"
    bootstrap = root / "service/configuration.yaml"
    secret = root / "service/cursor.secret"
    secret.parent.mkdir(parents=True)
    secret.write_bytes(b"configuration-integration-secret-32-bytes")
    secret.chmod(0o600)
    settings = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "service_id": "configuration-integration",
        "grpc": {
            "host": "127.0.0.1",
            "port": port,
        },
        "repository": {
            "sqlite_path": str(database),
            "backup_root": str(root / "backups"),
        },
        "nats": {
            "servers": [os.environ.get("WIND_HUB_TEST_NATS_URL", "nats://127.0.0.1:1")],
            "subject": "wh.config.changed",
            "connect_timeout_ms": 1000,
        },
        "security": {
            "cursor_secret_file": str(secret),
            "token_sources": {
                "client-viewer": ensure_bearer("viewer"),
                "client-ops": ensure_bearer("ops"),
                "client-deployer": ensure_bearer("deployer"),
            },
            "permissions": {
                "client-viewer": ["query"],
                "client-ops": ["query", "publish"],
                "client-deployer": ["query", "publish", "admin"],
            },
        },
        "limits": {
            "max_request_bytes": 4 * 1024 * 1024,
            "max_bundle_bytes": 8 * 1024 * 1024,
            "max_documents_per_publication": 100,
            "max_dependencies": 1000,
            "candidate_ttl_seconds": 3600,
            "write_timeout_ms": 5000,
            "max_candidates": 32,
            "max_backup_jobs": 2,
            "max_concurrent_queries": 16,
            "page_size": 2,
            "max_workers": 8,
        },
    }
    bootstrap.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    bootstrap.chmod(0o600)
    process_tokens = {
        ensure_bearer(identity).removeprefix("env:"): os.environ[
            ensure_bearer(identity).removeprefix("env:")
        ]
        for identity in ("viewer", "ops", "deployer")
    }
    cli_path = root / "cli.yaml"
    cli_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "cli": {"timeout_seconds": 10, "output_format": "json"},
                "services": {
                    "configuration": {
                        "grpc_endpoint": f"127.0.0.1:{port}",
                        "manager": "process",
                        "target": "configuration",
                        "process": {
                            "argv": [
                                sys.executable,
                                "-m",
                                "configuration_service",
                                "--config",
                                str(bootstrap),
                            ],
                            "cwd": str(SRC),
                            "env": {"PYTHONPATH": str(SRC), **process_tokens},
                            "state_dir": str(root / "process-state"),
                            "log_file": str(root / "configuration.log"),
                        },
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cli_path.chmod(0o600)
    return LiveConfiguration(root, pki, port, cli_path, bootstrap, database)
