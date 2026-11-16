"""真实验收：containerd/iSulad 环境可用性、真实 CLI 子进程。

containerd/iSulad 仅在真实守护进程可用时执行，不以 mock 替代。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from cli.process_manager import manage_service_process
from cli.runtime import ServiceTarget

pytestmark = [pytest.mark.system, pytest.mark.real_service]


def _target(manager: str, target_name: str) -> ServiceTarget:
    return ServiceTarget(
        service_id="svc",
        grpc_endpoint=None,
        tls_profile=None,
        manager=manager,
        target=target_name,
        namespace="default",
        manager_socket="/run/containerd/containerd.sock",
        isula_binary=None,
        process=None,
        timeout=5,
    )


async def test_containerd_native_grpc_or_not_run() -> None:
    socket = Path("/run/containerd/containerd.sock")
    if not socket.exists() or shutil.which("ctr") is None:
        pytest.skip("NOT_RUN: 本机无 containerd socket/CLI，UC-02 containerd 9 宫格未执行")
    result = await manage_service_process(
        _target("containerd", "wind-hub-acceptance-absent"), "status"
    )
    raise AssertionError(f"unexpected status from absent task: {result}")  # pragma: no cover


async def test_isulad_or_not_run() -> None:
    if shutil.which("isula") is None and not Path("/usr/bin/isula").exists():
        pytest.skip("NOT_RUN: 本机无 iSulad，UC-02 isulad 9 宫格未执行")


def test_cli_subprocess_json_envelope_and_exit_code(cli_env: Any) -> None:
    """真实子进程入口（python -m cli.main，独立解释器、独立事件循环）。"""
    result = cli_env.subprocess_invoke("net", "tcp", "127.0.0.1", "1", "--timeout", "0.2")
    assert result.exit_code == 3
    envelope = result.json()
    assert envelope["schema_version"] == 1
    assert envelope["command"] == "net tcp"
    assert envelope["data"]["tcp_state"] in {"CLOSED", "TIMEOUT"}
