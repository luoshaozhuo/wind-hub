"""CLI v1 验收共享 fixture。

- ``certs``：临时 CA / 服务器 / 客户端证书（cryptography 真实签发，不预生成静态资产）；
- ``FakeRpcServer``：真实 grpc.aio 服务器，按冻结 proto 描述符注册六个服务的全部
  Unary 方法，测试用 ``respond`` 注入响应或 gRPC 错误；这不是 mock daemon，
  进程管理（containerd/iSulad/systemd）的真实验收另行处理；
- ``cli_env``：临时 cli.yaml + 真实 mTLS 端点 + Typer CliRunner/子进程两种调用方式。
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import socket
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from google.protobuf import message_factory
from grpc import aio as grpc_aio
from typer.testing import CliRunner

from contracts import (
    collector_pb2,
    commander_pb2,
    common_pb2,
    data_generator_pb2,
    industrial_gateway_pb2,
    sink_service_pb2,
)

_PB2_MODULES = (
    common_pb2,
    collector_pb2,
    commander_pb2,
    sink_service_pb2,
    industrial_gateway_pb2,
    data_generator_pb2,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_certs(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wind-hub-test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    def leaf(name: str, *, client: bool) -> tuple[Path, Path]:
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.ExtendedKeyUsage(
                    [
                        x509.oid.ExtendedKeyUsageOID.SERVER_AUTH
                        if not client
                        else x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH
                    ]
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
                ),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )
        key_path = directory / f"{name}.key"
        cert_path = directory / f"{name}.crt"
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return cert_path, key_path

    ca_path = directory / "ca.crt"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    server_crt, server_key = leaf("server", client=False)
    client_crt, client_key = leaf("client", client=True)
    return {
        "ca": ca_path,
        "server_crt": server_crt,
        "server_key": server_key,
        "client_crt": client_crt,
        "client_key": client_key,
    }


@pytest.fixture
def certs(tmp_path: Path) -> dict[str, Path]:
    return _write_certs(tmp_path / "certs")


Behavior = Callable[[Any, grpc_aio.ServicerContext], Awaitable[Any]]


@dataclass
class CallRecord:
    path: str
    request: Any


class FakeRpcServer:
    """真实 grpc.aio 服务器；行为表按 /package.Service/Method 注册。"""

    def __init__(self, cert_files: dict[str, Path]) -> None:
        self.cert_files = cert_files
        self.port = _free_port()
        self.behaviors: dict[str, Behavior] = {}
        self.errors: dict[str, grpc.StatusCode] = {}
        self.calls: list[CallRecord] = []
        self._server: grpc_aio.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def respond(self, method: str, behavior: Behavior) -> None:
        self.behaviors[method] = behavior

    def respond_value(self, method: str, response: Any) -> None:
        async def behavior(request: Any, context: grpc_aio.ServicerContext) -> Any:
            return response

        self.respond(method, behavior)

    def fail(self, method: str, code: grpc.StatusCode, details: str = "injected") -> None:
        async def behavior(request: Any, context: grpc_aio.ServicerContext) -> Any:
            await context.abort(code, details)

        self.respond(method, behavior)

    def start_thread(self) -> None:
        """在专用线程的事件循环中运行——CLI 在主线程独立循环发起真实 TCP/mTLS 调用。"""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="fake-rpc-server", daemon=True
        )
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._build(), self._loop)
        future.result(10)

    def stop_thread(self) -> None:
        if self._loop is None or self._thread is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        future.result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop = None
        self._thread = None

    async def _build(self, *, require_client: bool = True) -> None:
        server = grpc_aio.server()
        for module in _PB2_MODULES:
            for service in module.DESCRIPTOR.services_by_name.values():
                handlers: dict[str, grpc.RpcMethodHandler] = {}
                for method in service.methods:
                    if method.client_streaming or method.server_streaming:
                        continue  # v1 CLI 只使用 Unary RPC
                    request_cls = message_factory.GetMessageClass(method.input_type)
                    response_cls = message_factory.GetMessageClass(method.output_type)
                    path = f"/{service.full_name}/{method.name}"
                    handlers[method.name] = grpc.unary_unary_rpc_method_handler(
                        self._make(path, request_cls),
                        request_deserializer=request_cls.FromString,
                        response_serializer=response_cls.SerializeToString,
                    )
                server.add_generic_rpc_handlers(
                    (grpc.method_handlers_generic_handler(service.full_name, handlers),)
                )
        credentials = grpc.ssl_server_credentials(
            [
                (
                    self.cert_files["server_key"].read_bytes(),
                    self.cert_files["server_crt"].read_bytes(),
                )
            ],
            root_certificates=self.cert_files["ca"].read_bytes(),
            require_client_auth=require_client,
        )
        server.add_secure_port(f"127.0.0.1:{self.port}", credentials)
        await server.start()
        self._server = server

    async def _shutdown(self) -> None:
        if self._server is not None:
            await self._server.stop(grace=1)
            self._server = None

    def _make(self, path: str, request_cls: Any) -> Callable[..., Awaitable[Any]]:
        async def dispatch(raw_request: Any, context: grpc_aio.ServicerContext) -> Any:
            request = request_cls() if raw_request is None else raw_request
            self.calls.append(CallRecord(path, request))
            behavior = self.behaviors.get(path)
            if behavior is None:
                await context.abort(grpc.StatusCode.UNIMPLEMENTED, f"no test behavior for {path}")
                raise AssertionError("unreachable after abort")
            return await behavior(request, context)

        return dispatch

    def method_path(self, service_name: str, method: str) -> str:
        for module in _PB2_MODULES:
            service = module.DESCRIPTOR.services_by_name.get(service_name)
            if service is not None:
                return f"/{service.full_name}/{method}"
        raise KeyError(service_name)


@pytest.fixture
def fake_server(certs: dict[str, Path]):
    server = FakeRpcServer(certs)
    server.start_thread()
    try:
        yield server
    finally:
        server.stop_thread()


def _write_cli_yaml(
    path: Path, cert_files: dict[str, Path], port: int, *, tls: bool = True
) -> Path:
    services = "\n".join(
        f"""  {name}:
    grpc_endpoint: "localhost:{port}"
    tls_profile: mtls"""
        for name in (
            "collector",
            "commander",
            "sink_realtime",
            "sink_reliable",
            "gateway",
            "data_generator",
        )
    )
    profile = ""
    if tls:
        profile = f"""
tls_profiles:
  mtls:
    ca_cert: {cert_files['ca']}
    client_cert: {cert_files['client_crt']}
    client_key: {cert_files['client_key']}
"""
    path.write_text(
        f"""schema_version: 1
cli:
  timeout_seconds: 5
  output_format: table
services:
{services}
{profile}
""",
        encoding="utf-8",
    )
    return path


@dataclass
class CliResult:
    exit_code: int
    stdout: str

    def json(self) -> dict[str, Any]:
        lines = [line for line in self.stdout.splitlines() if line.startswith("{")]
        assert lines, f"no JSON envelope in output: {self.stdout!r}"
        result: dict[str, Any] = json.loads(lines[-1])
        return result


@dataclass
class CliEnv:
    config_path: Path
    server: FakeRpcServer
    invoke: Callable[..., CliResult]
    subprocess_invoke: Callable[..., CliResult]


@pytest.fixture
def cli_env(tmp_path: Path, certs: dict[str, Path], fake_server: FakeRpcServer):
    import cli.main as main_module

    config_path = _write_cli_yaml(tmp_path / "cli.yaml", certs, fake_server.port)
    runner = CliRunner()
    previous = (
        main_module._config,
        main_module._format,
        main_module._runtime,
        main_module._shell_loop,
        main_module._active_rpc,
    )

    def invoke(*arguments: str, format: str = "json") -> CliResult:
        args = ["--config", str(config_path)]
        if format:
            args.extend(["--format", format])
        args.extend(arguments)
        result = runner.invoke(main_module.app, args, catch_exceptions=False)
        return CliResult(result.exit_code, result.stdout)

    def subprocess_invoke(*arguments: str) -> CliResult:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "cli.main",
                "--config",
                str(config_path),
                "--format",
                "json",
                *arguments,
            ],
            cwd=Path(__file__).resolve().parents[2] / "src",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return CliResult(completed.returncode, completed.stdout)

    yield CliEnv(config_path, fake_server, invoke, subprocess_invoke)

    (
        main_module._config,
        main_module._format,
        main_module._runtime,
        main_module._shell_loop,
        main_module._active_rpc,
    ) = previous
