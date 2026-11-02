"""真实验收：systemd D-Bus、containerd/iSulad 环境可用性、真实 CLI 子进程。

containerd/iSulad 的 9 宫格验收在缺失守护进程的环境下显式 NOT_RUN（skip），
绝不以 mock daemon 顶替；systemd 在非特权环境下只验收只读 status 与真实权限拒绝路径。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from cli.process_manager import manage
from cli.runtime import ServiceTarget

pytestmark = [pytest.mark.system, pytest.mark.real_service]


def _target(manager: str, target_name: str) -> ServiceTarget:
    return ServiceTarget(
        service_id="svc",
        grpc_endpoint=None,
        rpc_via=None,
        tls_profile=None,
        manager=manager,
        target=target_name,
        namespace="default",
        manager_socket="/run/containerd/containerd.sock",
        isula_binary=None,
        systemd_scope="system",
        timeout=5,
    )


async def test_systemd_status_real_dbus_unit() -> None:
    if not Path("/run/dbus/system_bus_socket").exists():
        pytest.skip("NOT_RUN: 本机无 system D-Bus")
    result = await manage(_target("systemd", "dbus.service"), "status")
    assert result["manager"] == "systemd"
    assert result["active_state"] == "active"
    assert result["status"] == "ACTIVE"


async def test_systemd_start_permission_denied_is_real_error() -> None:
    if not Path("/run/dbus/system_bus_socket").exists():
        pytest.skip("NOT_RUN: 本机无 system D-Bus")
    if os.geteuid() == 0:
        pytest.skip("root 环境下不会触发 Polkit 拒绝路径")
    with pytest.raises(Exception, match="[Aa]ccess denied|auth"):
        await manage(_target("systemd", "dbus.service"), "start")


async def test_containerd_native_grpc_or_not_run() -> None:
    socket = Path("/run/containerd/containerd.sock")
    if not socket.exists() or shutil.which("ctr") is None:
        pytest.skip("NOT_RUN: 本机无 containerd socket/CLI，UC-02 containerd 9 宫格未执行")
    result = await manage(_target("containerd", "wind-hub-acceptance-absent"), "status")
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
