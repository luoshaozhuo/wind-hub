"""System e2e 共享工具：真实 CLI 子进程（独立解释器）+ cli.yaml 生成。

设计 14.1：System Test 通过真实 CLI 进程和真实通信链路执行。
对端是真实 grpc.aio + mTLS 服务器（见 conftest.FakeRpcServer），业务行为由
测试注入；这验证 CLI 客户端行为，不替代五服务生产服务端端到端验收。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[2] / "src"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_cli_yaml(
    path: Path,
    cert_files: dict[str, Path],
    endpoints: dict[str, str],
    *,
    managers: dict[str, dict[str, str]] | None = None,
    timeout: float = 5.0,
    prompt: str = "wind-hub> ",
) -> Path:
    managers = managers or {}
    blocks = []
    for name, endpoint in endpoints.items():
        block = f'  {name}:\n    grpc_endpoint: "{endpoint}"\n    tls_profile: mtls\n'
        if name in managers:
            for key, value in managers[name].items():
                block += f"    {key}: {value}\n"
        blocks.append(block)
    for name, manager in managers.items():
        if name in endpoints:
            continue
        block = f"  {name}:\n"
        for key, value in manager.items():
            block += f"    {key}: {value}\n"
        blocks.append(block)
    path.write_text(
        f"""schema_version: 1
cli:
  timeout_seconds: {timeout}
  output_format: json
  prompt: {json.dumps(prompt)}
services:
{''.join(blocks)}
tls_profiles:
  mtls:
    ca_cert: {cert_files['ca']}
    client_cert: {cert_files['client_crt']}
    client_key: {cert_files['client_key']}
""",
        encoding="utf-8",
    )
    return path


def run_cli(
    config: Path | None,
    *arguments: str,
    timeout: float = 30,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, "-m", "cli.main"]
    if config is not None:
        args += ["--config", str(config)]
    args += ["--format", "json", *arguments]
    return subprocess.run(
        args,
        cwd=SRC,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def last_json(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.startswith("{")]
    assert lines, f"no JSON envelope in output: {stdout!r}"
    result: dict[str, Any] = json.loads(lines[-1])
    return result


def method_path(package: str, service: str, method: str) -> str:
    return f"/{package}.{service}/{method}"


_PB2 = None


def respond_ok(server: Any, service: str, method: str, **fields: Any) -> str:
    """Register an empty (or field-populated) default response for a frozen RPC."""
    global _PB2
    if _PB2 is None:
        from contracts import (
            collector_pb2,
            commander_pb2,
            common_pb2,
            data_generator_pb2,
            industrial_gateway_pb2,
            sink_service_pb2,
        )

        _PB2 = (
            common_pb2,
            collector_pb2,
            commander_pb2,
            sink_service_pb2,
            industrial_gateway_pb2,
            data_generator_pb2,
        )
    descriptor = None
    for module in _PB2:
        descriptor = module.DESCRIPTOR.services_by_name.get(service)
        if descriptor is not None:
            break
    assert descriptor is not None, service
    method_descriptor = descriptor.methods_by_name[method]
    from google.protobuf.message_factory import GetMessageClass

    response_cls = GetMessageClass(method_descriptor.output_type)
    path = server.method_path(service, method)
    server.respond_value(path, response_cls(**fields))
    return path
