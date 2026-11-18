"""Real Configuration Service process, PKI and CLI integration harness."""

from __future__ import annotations

import datetime
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
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from contracts import common_pb2_grpc, configuration_repository_pb2_grpc
from tests.commander.grpc_harness import Pki

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
        credentials = grpc.ssl_channel_credentials(
            root_certificates=(self.pki.tmp / f"{ca}.crt").read_bytes(),
            private_key=(self.pki.tmp / f"{identity}.key").read_bytes(),
            certificate_chain=(self.pki.tmp / f"{identity}.crt").read_bytes(),
        )
        return grpc.aio.secure_channel(
            f"127.0.0.1:{self.port}",
            credentials,
            options=(("grpc.ssl_target_name_override", "localhost"),),
        )

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


def issue_client_identity(pki: Pki, identity: str, common_name: str | None) -> None:
    now = datetime.datetime.now(datetime.UTC)
    ca_key = serialization.load_pem_private_key((pki.tmp / "ca.key").read_bytes(), password=None)
    ca_cert = x509.load_pem_x509_certificate((pki.tmp / "ca.crt").read_bytes())
    key = ec.generate_private_key(ec.SECP256R1())
    subject = [] if common_name is None else [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name(subject))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    (pki.tmp / f"{identity}.crt").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (pki.tmp / f"{identity}.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def issue_client_without_common_name(pki: Pki) -> None:
    issue_client_identity(pki, "no-cn", None)


def reserve_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_live_configuration(root: Path, pki: Pki, port: int) -> LiveConfiguration:
    issue_client_without_common_name(pki)
    issue_client_identity(pki, "blank-cn", "   ")
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
            "tls": {
                "ca_file": str(pki.tmp / "ca.crt"),
                "cert_file": str(pki.tmp / "server.crt"),
                "key_file": str(pki.tmp / "server.key"),
            },
        },
        "repository": {
            "sqlite_path": str(database),
            "backup_root": str(root / "backups"),
        },
        "security": {
            "cursor_secret_file": str(secret),
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
                        "tls_profile": "admin",
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
                "tls_profiles": {
                    "admin": {
                        "ca_cert": str(pki.tmp / "ca.crt"),
                        "client_cert": str(pki.tmp / "deployer.crt"),
                        "client_key": str(pki.tmp / "deployer.key"),
                    },
                    "query": {
                        "ca_cert": str(pki.tmp / "ca.crt"),
                        "client_cert": str(pki.tmp / "viewer.crt"),
                        "client_key": str(pki.tmp / "viewer.key"),
                    },
                    "publish": {
                        "ca_cert": str(pki.tmp / "ca.crt"),
                        "client_cert": str(pki.tmp / "ops.crt"),
                        "client_key": str(pki.tmp / "ops.key"),
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cli_path.chmod(0o600)
    return LiveConfiguration(root, pki, port, cli_path, bootstrap, database)
