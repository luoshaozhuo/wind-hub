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

import shutil
import subprocess
import time
from pathlib import Path

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.yml"
PROJECT_NAME = "wind-hub-test"

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
            PROJECT_NAME,
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def compose_up() -> None:
    """后台启动全部测试服务。

    Raises:
        ComposeError: compose up 失败（镜像缺失、端口冲突等）。
    """
    result = _compose("up", "-d", "--wait", "--wait-timeout", "120")
    if result.returncode != 0:
        raise ComposeError(
            f"docker compose up failed ({result.returncode}): "
            f"{result.stdout}\n{result.stderr}"
        )


def compose_down() -> None:
    """停止并删除测试服务与数据卷（幂等——未启动时也是成功）。"""
    result = _compose("down", "-v", "--remove-orphans")
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
        raise ComposeError(
            f"services not healthy within {timeout_s}s: {sorted(pending)}"
        )


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
