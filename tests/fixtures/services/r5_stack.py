"""Round 5 真实服务栈生命周期（独立 Docker Compose project）。

与 :mod:`tests.fixtures.services.compose` 的 wind-hub-test 栈完全隔离：

- project 名 ``wind-hub-r5-<短随机>``，宿主端口动态分配且只绑 loopback；
- 服务：NATS 2.10(-js) / Redis 7 / TDengine 3.3.8.8(taosAdapter) /
  Kafka 3.9.1(KRaft)，版本固定在 docker-compose.r5.yml；
- 生命周期只作用于本 project：``down -v --remove-orphans`` 不触碰用户
  已有容器，不执行全局 prune；
- 故障注入只 ``docker pause/unpause``、``stop/start``、``network
  disconnect/connect`` 本 project 的容器。

docker 不可用或启动失败时由调用方转成明确 skip / 失败，绝不退化为 mock。
"""

from __future__ import annotations

import base64
import json
import shutil
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

COMPOSE_FILE = Path(__file__).resolve().parent / "docker-compose.r5.yml"

TDENGINE_DATABASES = ("wind_hub_r5", "wind_hub_r5b")


class R5StackError(RuntimeError):
    """R5 真实服务栈启动/故障注入失败。"""


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False)
    return probe.returncode == 0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True, slots=True)
class R5Stack:
    project: str
    nats: str
    redis: str
    tdengine_rest: str
    kafka: str
    nats_monitor_port: int

    def container(self, service: str) -> str:
        return f"{self.project}-{service}-1"

    def tdengine_sql(self, sql: str, *, database: str | None = None, timeout: float = 10.0) -> dict:
        """对 taosAdapter 执行 REST SQL，返回解析后的 JSON（仅测试支撑用）。"""
        suffix = f"/{database}" if database else ""
        url = f"http://{self.tdengine_rest}/rest/sql{suffix}"
        token = base64.b64encode(b"root:taosdata").decode("ascii")
        request = urllib.request.Request(
            url,
            data=sql.encode("utf-8"),
            headers={"Authorization": f"Basic {token}", "Content-Type": "text/plain"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = json.loads(response.read())
        if not isinstance(body, dict) or body.get("code") != 0:
            raise R5StackError(f"TDengine SQL rejected: {body!r}")
        return body

    def docker(self, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        """对本 project 的容器执行 docker 管理命令。"""
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def pause(self, service: str) -> None:
        result = self.docker("pause", self.container(service))
        if result.returncode != 0:
            raise R5StackError(f"pause {service}: {result.stderr.strip()}")

    def unpause(self, service: str) -> None:
        result = self.docker("unpause", self.container(service))
        if result.returncode != 0:
            raise R5StackError(f"unpause {service}: {result.stderr.strip()}")

    def stop_container(self, service: str, *, timeout: float = 30.0) -> None:
        result = self.docker("stop", "-t", "5", self.container(service), timeout=timeout)
        if result.returncode != 0:
            raise R5StackError(f"stop {service}: {result.stderr.strip()}")

    def start_container(self, service: str, *, timeout: float = 120.0) -> None:
        result = self.docker("start", self.container(service), timeout=timeout)
        if result.returncode != 0:
            raise R5StackError(f"start {service}: {result.stderr.strip()}")

    def disconnect_network(self, service: str) -> None:
        result = self.docker(
            "network", "disconnect", "-f", f"{self.project}_default", self.container(service)
        )
        if result.returncode != 0:
            raise R5StackError(f"network disconnect {service}: {result.stderr.strip()}")

    def connect_network(self, service: str) -> None:
        result = self.docker(
            "network", "connect", f"{self.project}_default", self.container(service)
        )
        if result.returncode != 0:
            raise R5StackError(f"network connect {service}: {result.stderr.strip()}")


def _compose(
    project: str, env: dict[str, str], *args: str, timeout: float = 240.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", project, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


def _wait_tcp(host: str, port: int, *, deadline: float) -> None:
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.5)
    raise R5StackError(f"TCP {host}:{port} not reachable before deadline")


def up_stack() -> R5Stack:
    """启动 R5 真实服务栈并完成应用层就绪确认。"""
    if not docker_available():
        raise R5StackError("docker CLI or daemon unavailable")

    import secrets

    project = f"wind-hub-r5-{secrets.token_hex(4)}"
    ports = {
        "R5_PROJECT": project,
        "R5_NATS_PORT": str(free_port()),
        "R5_NATS_MON_PORT": str(free_port()),
        "R5_REDIS_PORT": str(free_port()),
        "R5_TDENGINE_PORT": str(free_port()),
        "R5_KAFKA_PORT": str(free_port()),
    }
    import os

    env = {**os.environ, **ports}
    try:
        result = _compose(project, env, "up", "-d", "--wait", "--wait-timeout", "180")
        if result.returncode != 0:
            _compose(project, env, "down", "-v", "--remove-orphans", timeout=60.0)
            raise R5StackError(
                f"docker compose up failed ({result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )
        stack = R5Stack(
            project=project,
            nats=f"nats://127.0.0.1:{ports['R5_NATS_PORT']}",
            redis=f"127.0.0.1:{ports['R5_REDIS_PORT']}",
            tdengine_rest=f"127.0.0.1:{ports['R5_TDENGINE_PORT']}",
            kafka=f"127.0.0.1:{ports['R5_KAFKA_PORT']}",
            nats_monitor_port=int(ports["R5_NATS_MON_PORT"]),
        )
        # compose healthcheck 之外的宿主侧协议就绪确认。
        deadline = time.monotonic() + 60.0
        _wait_tcp("127.0.0.1", int(ports["R5_NATS_PORT"]), deadline=deadline)
        _wait_tcp("127.0.0.1", int(ports["R5_REDIS_PORT"]), deadline=deadline)
        _wait_tcp("127.0.0.1", int(ports["R5_TDENGINE_PORT"]), deadline=deadline)
        _wait_tcp("127.0.0.1", int(ports["R5_KAFKA_PORT"]), deadline=deadline)
        _wait_tdengine_ready(stack)
        for database in TDENGINE_DATABASES:
            stack.tdengine_sql(f"CREATE DATABASE IF NOT EXISTS {database}")
        return stack
    except Exception:
        down_project(project, env)
        raise


def _wait_tdengine_ready(stack: R5Stack) -> None:
    deadline = time.monotonic() + 60.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            stack.tdengine_sql("SELECT SERVER_VERSION()")
            return
        except Exception as exc:  # taosAdapter 起来到可查询之间有窗口
            last_error = exc
            time.sleep(1.0)
    raise R5StackError(f"taosAdapter not queryable: {last_error}")


def down_project(project: str, env: dict[str, str]) -> None:
    result = _compose(project, env, "down", "-v", "--remove-orphans", timeout=120.0)
    if result.returncode != 0:
        raise R5StackError(
            f"docker compose down failed ({result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )


def down_stack(stack: R5Stack) -> None:
    # project 名已编码到容器/网络名；down 不使用宿主端口变量。
    import os

    env = {**os.environ, "R5_PROJECT": stack.project}
    down_project(stack.project, env)
