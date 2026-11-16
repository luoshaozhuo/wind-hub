"""测试用 Docker Compose 生命周期管理。

只管理 ``tests/fixtures/services/docker-compose.yml`` 中声明的 Redis。
pytest session fixture 的使用模式为::

    compose_up()
    wait_services_healthy(...)
    yield
    compose_down()

约定：

- 不自动 sudo；docker 不可用（无命令或 daemon 不可达）时
  :func:`docker_available` 返回 False，由调用方转成明确的 skip；
- ``up``/``down`` 失败抛 :class:`ComposeError`，不在本层静默；
- 健康判定以 Compose 文件的 healthcheck 为准（``docker inspect``
  读取 ``State.Health.Status``），不做应用层假阳性探测。
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
PROJECT_NAME = "wind-hub-test"
_PROJECT_NAME: str | None = None
_HOST_PORT: int | None = None

#: 服务名 → （健康等待默认超时秒，容器内检查由 compose healthcheck 定义）
SERVICE_HEALTH_TIMEOUT_S = 120.0


class ComposeError(RuntimeError):
    """docker compose 命令执行失败（stdout/stderr 并入消息）。"""


def docker_available() -> bool:
    """docker CLI 与 daemon 均可用时返回 True。"""
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    return probe.returncode == 0


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "-p",
            _PROJECT_NAME or PROJECT_NAME,
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env={**os.environ, "WIND_HUB_TEST_REDIS_PORT": str(_HOST_PORT or 6379)},
    )


def _free_port() -> int:
    """Select a loopback port without affecting unrelated Docker projects."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def compose_up() -> int:
    """Start a managed Redis on a dynamic host port; retry ephemeral port races."""
    global _HOST_PORT, _PROJECT_NAME
    if _HOST_PORT is not None:
        raise ComposeError("Redis Compose already managed by this process")
    _PROJECT_NAME = f"{PROJECT_NAME}-{secrets.token_hex(4)}"
    for _attempt in range(5):
        _HOST_PORT = _free_port()
        result = _compose("up", "-d", "--wait", "--wait-timeout", "120")
        if result.returncode == 0:
            return _HOST_PORT
        # Own project only; never stop another project already using a port.
        _compose("down", "-v", "--remove-orphans")
        if "port is already allocated" not in result.stderr.lower() and (
            "address already in use" not in result.stderr.lower()
            and "bind:" not in result.stderr.lower()
        ):
            _HOST_PORT = None
            _PROJECT_NAME = None
            raise ComposeError(
                f"docker compose up failed ({result.returncode}): "
                f"{result.stdout}\\n{result.stderr}"
            )
    _HOST_PORT = None
    _PROJECT_NAME = None
    raise ComposeError(f"Redis port collision persisted after {_attempt + 1} attempts")


def compose_down() -> None:
    """Stop only this test project and release the selected port."""
    global _HOST_PORT, _PROJECT_NAME
    result = _compose("down", "-v", "--remove-orphans")
    _HOST_PORT = None
    _PROJECT_NAME = None
    if result.returncode != 0:
        raise ComposeError(
            f"docker compose down failed ({result.returncode}): "
            f"{result.stdout}\n{result.stderr}"
        )


def _container_id(service: str) -> str | None:
    result = _compose("ps", "-q", service)
    if result.returncode != 0:
        return None
    container_id = result.stdout.strip()
    return container_id or None


def service_health(service: str) -> str | None:
    """读取单个服务的 healthcheck 状态（healthy/unhealthy/starting/none）。"""
    container_id = _container_id(service)
    if container_id is None:
        return None
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_id],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def wait_services_healthy(
    services: list[str],
    *,
    timeout_s: float = SERVICE_HEALTH_TIMEOUT_S,
) -> None:
    """轮询直到全部服务 healthcheck 通过。

    Raises:
        ComposeError: 超时或服务进入 unhealthy 状态。
    """
    deadline = time.monotonic() + timeout_s
    pending = set(services)
    while pending and time.monotonic() < deadline:
        for service in list(pending):
            status = service_health(service)
            if status == "healthy":
                pending.discard(service)
            elif status == "unhealthy":
                raise ComposeError(f"service '{service}' is unhealthy")
        if pending:
            time.sleep(1.0)
    if pending:
        raise ComposeError(f"services not healthy within {timeout_s}s: {sorted(pending)}")


def compose_stop_service(service: str) -> None:
    """停止单个服务（故障注入用）。

    Raises:
        ComposeError: 停止失败。
    """
    result = _compose("stop", service)
    if result.returncode != 0:
        raise ComposeError(
            f"docker compose stop {service} failed: {result.stdout}\n{result.stderr}"
        )


def compose_start_service(service: str) -> None:
    """重新启动单个服务并等待其 healthcheck 通过（故障恢复用）。"""
    result = _compose("start", service)
    if result.returncode != 0:
        raise ComposeError(
            f"docker compose start {service} failed: {result.stdout}\n{result.stderr}"
        )
    wait_services_healthy([service])
