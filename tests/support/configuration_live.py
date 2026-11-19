"""Real Configuration Service process and token-authorized CLI harness."""

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

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


class _BearerInterceptor(grpc.aio.UnaryUnaryClientInterceptor):
    def __init__(self, token: str) -> None:
        self.token = token

    async def intercept_unary_unary(
        self, continuation: Any, client_call_details: grpc.aio.ClientCallDetails, request: Any
    ) -> Any:
        metadata = list(client_call_details.metadata or ())
        metadata.append(("authorization", f"Bearer {self.token}"))
        updated = grpc.aio.ClientCallDetails(
            client_call_details.method,
            client_call_details.timeout,
            metadata,
            client_call_details.credentials,
            client_call_details.wait_for_ready,
        )
        return await continuation(updated, request)


@dataclass
class LiveConfiguration:
    root: Path
    port: int
    tokens: dict[str, str]
    cli_path: Path
    bootstrap_path: Path
    database_path: Path

    def channel(self, identity: str = "deployer") -> grpc.aio.Channel:
        token = self.tokens.get(identity)
        interceptors = [_BearerInterceptor(token)] if token is not None else []
        return grpc.aio.insecure_channel(f"127.0.0.1:{self.port}", interceptors=interceptors)

    def stubs(
        self, identity: str = "deployer"
    ) -> tuple[
        configuration_repository_pb2_grpc.ConfigurationRepositoryServiceStub,
        common_pb2_grpc.CommonServiceStub,
        grpc.aio.Channel,
    ]:
        channel = self.channel(identity)
        return (
            configuration_repository_pb2_grpc.ConfigurationRepositoryServiceStub(channel),
            common_pb2_grpc.CommonServiceStub(channel),
            channel,
        )

    def cli(
        self, *arguments: str, check: bool = True, config: Path | None = None
    ) -> dict[str, Any]:
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
            env={**os.environ, "PYTHONPATH": str(SRC)},
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


def write_live_configuration(root: Path, port: int) -> LiveConfiguration:
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
        "nats": {
            "servers": ["nats://127.0.0.1:4222"],
            "subject": "wh.config.changed",
            "connect_timeout_ms": 2000,
        },
        "repository": {
            "sqlite_path": str(database),
            "backup_root": str(root / "backups"),
        },
        "security": {
            "cursor_secret_file": str(secret),
            "token_sources": {
                "client-viewer": "env:WIND_HUB_CONFIG_VIEWER_TOKEN",
                "client-ops": "env:WIND_HUB_CONFIG_OPS_TOKEN",
                "client-deployer": "env:WIND_HUB_CONFIG_CLI_ADMIN_TOKEN",
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
    tokens = {
        "viewer": "configuration-viewer-credential-000001",
        "ops": "configuration-operator-credential-000002",
        "deployer": "configuration-deployer-credential-000003",
    }
    for role, token in tokens.items():
        variable = (
            "WIND_HUB_CONFIG_CLI_ADMIN_TOKEN"
            if role == "deployer"
            else f"WIND_HUB_CONFIG_{role.upper()}_TOKEN"
        )
        os.environ[variable] = token
    bootstrap.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    bootstrap.chmod(0o600)
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
                            "env": {"PYTHONPATH": str(SRC)},
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
    return LiveConfiguration(root, port, tokens, cli_path, bootstrap, database)
