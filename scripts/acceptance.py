#!/usr/bin/env python
"""wind-hub 一键手工验收环境编排器。

一条命令拉起完整真实链路：40 台模拟 PLC（scripts/simulator）→
Collector → Commander → Web Server（可选 Vite 前端 dev server），
配置由 gen_config.py 生成到 ``run/acceptance/config``（真实 Core
schema，启用 CSV / Redis / Modbus SCADA 三种真实 sink）。

用法（仓库根目录）::

    python3 scripts/acceptance.py start [--with-redis] [--with-frontend]
    python3 scripts/acceptance.py status
    python3 scripts/acceptance.py stop

- ``--with-redis``：经 docker 启动临时 redis:7-alpine 容器并启用
  Redis sink（无 docker 时直接报错，不静默降级）；
- ``--with-frontend``：启动 ``npm run dev``（需先在 src/web_admin
  执行过 npm install）。

状态写入 ``run/acceptance/acceptance.json``（各进程 pid / 端口 /
写操作 API token），日志在 ``run/acceptance/logs/``。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = REPO_ROOT / "run" / "acceptance"
STATE_PATH = RUN_DIR / "acceptance.json"
CONFIG_DIR = RUN_DIR / "config"
LOG_DIR = RUN_DIR / "logs"

WEB_PORT = 8000
FRONTEND_PORT = 5173
COLLECTOR_GRPC_PORT = 50051
COMMANDER_GRPC_PORT = 50052
MODBUS_SINK_PORT = 1502
REDIS_PORT = 6379
REDIS_CONTAINER = "wind-hub-acceptance-redis"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def _wait(predicate: object, what: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.3)
    raise RuntimeError(f"等待超时: {what}")


def _spawn(
    name: str,
    argv: list[str],
    env_extra: dict[str, str] | None = None,
    cwd: Path = REPO_ROOT,
) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = open(LOG_DIR / f"{name}.log", "w", encoding="utf-8")  # noqa: SIM115
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(  # noqa: S603
        argv,
        cwd=cwd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )
    return proc.pid


def _kill(pid: int, name: str) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.3)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
    print(f"警告: {name} (pid={pid}) 未在 15s 内退出，已 SIGKILL", file=sys.stderr)


def _start_redis() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError("--with-redis 需要 docker（启动临时 redis 容器）")
    subprocess.run(  # noqa: S603
        ["docker", "rm", "-f", REDIS_CONTAINER],
        check=False,
        capture_output=True,
    )
    subprocess.run(  # noqa: S603
        [
            "docker",
            "run",
            "-d",
            "--name",
            REDIS_CONTAINER,
            "-p",
            f"{REDIS_PORT}:6379",
            "redis:7-alpine",
        ],
        check=True,
        capture_output=True,
    )
    _wait(lambda: _port_open("127.0.0.1", REDIS_PORT), "redis 容器就绪")


def _stop_redis() -> None:
    if shutil.which("docker") is not None:
        subprocess.run(  # noqa: S603
            ["docker", "rm", "-f", REDIS_CONTAINER],
            check=False,
            capture_output=True,
        )


def _cmd_start(with_redis: bool, with_frontend: bool, foreground: bool = False) -> int:
    if STATE_PATH.is_file():
        print("验收环境已在运行（先 stop）", file=sys.stderr)
        return 1

    redis: tuple[str, int] | None = None
    if with_redis:
        print("启动临时 redis 容器…")
        _start_redis()
        redis = ("127.0.0.1", REDIS_PORT)

    print("生成验收配置…")
    csv_path = RUN_DIR / "archive.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    gen = [
        sys.executable,
        "scripts/simulator/gen_config.py",
        "--out",
        str(CONFIG_DIR),
        "--csv-path",
        str(csv_path),
        "--modbus-sink-port",
        str(MODBUS_SINK_PORT),
    ]
    if redis:
        gen += ["--redis", f"{redis[0]}:{redis[1]}"]
    subprocess.run(gen, check=True, cwd=REPO_ROOT)  # noqa: S603

    write_token = secrets.token_hex(8)
    state: dict[str, object] = {
        "write_token": write_token,
        "with_redis": with_redis,
        "processes": {},
    }

    print("启动模拟器（40 台设备）…")
    state["processes"]["simulator"] = _spawn(  # type: ignore[index]
        "simulator", [sys.executable, "scripts/simulator/main.py", "start", "--tick", "0.5"]
    )
    _wait(lambda: _port_open("127.0.0.1", 10502), "模拟器 modbus 端口")

    print("启动 Collector…")
    state["processes"]["collector"] = _spawn(  # type: ignore[index]
        "collector",
        [
            "wind-hub-collector",
            "--config",
            str(CONFIG_DIR),
            "--collector-id",
            "acceptance",
            "--grpc-port",
            str(COLLECTOR_GRPC_PORT),
        ],
    )
    _wait(lambda: _port_open("127.0.0.1", COLLECTOR_GRPC_PORT), "Collector gRPC")

    print("启动 Commander…")
    state["processes"]["commander"] = _spawn(  # type: ignore[index]
        "commander",
        [
            "wind-hub-commander",
            "--config",
            str(CONFIG_DIR),
            "--grpc-port",
            str(COMMANDER_GRPC_PORT),
        ],
    )
    _wait(lambda: _port_open("127.0.0.1", COMMANDER_GRPC_PORT), "Commander gRPC")

    print("启动 Web Server…")
    state["processes"]["web_server"] = _spawn(  # type: ignore[index]
        "web_server",
        ["wind-hub-web-server"],
        env_extra={
            "WIND_HUB_COLLECTOR_RPC": f"127.0.0.1:{COLLECTOR_GRPC_PORT}",
            "WIND_HUB_COMMANDER_RPC": f"127.0.0.1:{COMMANDER_GRPC_PORT}",
            "WIND_HUB_WEB_HOST": "127.0.0.1",
            "WIND_HUB_WEB_PORT": str(WEB_PORT),
            "WIND_HUB_WEB_WRITE_TOKEN": write_token,
        },
    )

    def _web_ready() -> bool:
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"http://127.0.0.1:{WEB_PORT}/api/v1/collector/info", timeout=1
            ):
                return True
        except Exception:  # noqa: BLE001
            return False

    _wait(_web_ready, "Web Server HTTP")

    if with_frontend:
        print("启动 Web Admin dev server…")
        state["processes"]["web_admin"] = _spawn(  # type: ignore[index]
            "web_admin",
            ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(FRONTEND_PORT)],
            cwd=REPO_ROOT / "src" / "web_admin",
        )
        _wait(lambda: _port_open("127.0.0.1", FRONTEND_PORT), "Vite dev server", timeout=90)

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    _print_summary(with_frontend, write_token)
    if foreground:
        _foreground_wait(state)
    return 0


def _foreground_wait(state: dict[str, object]) -> None:
    """前台驻留：阻塞到 SIGTERM/SIGINT，然后停止全部子进程并清理状态。

    供进程管理器（如 Playwright webServer）托管——管理器终止本进程即
    触发整链优雅停机，不残留孤儿进程。
    """
    stopped = False

    def _handle(_sig: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    print("前台驻留中（SIGTERM 停止整链）…", flush=True)
    while not stopped:
        time.sleep(0.5)
    print("收到停机信号，清理验收环境…", flush=True)
    for name, pid in state["processes"].items():  # type: ignore[union-attr]
        _kill(int(pid), str(name))
    if state.get("with_redis"):
        _stop_redis()
    STATE_PATH.unlink(missing_ok=True)


def _print_summary(with_frontend: bool, write_token: str) -> None:
    print()
    print("验收环境已就绪：")
    print(f"  Web API:     http://127.0.0.1:{WEB_PORT}/api/v1/collector/info")
    if with_frontend:
        print(f"  Web Admin:   http://127.0.0.1:{FRONTEND_PORT}")
    print(f"  写操作 token（X-API-Key）: {write_token}")
    print(f"  CSV 归档:    {RUN_DIR / 'archive.csv'}")
    print(f"  Modbus SCADA sink: 127.0.0.1:{MODBUS_SINK_PORT}（holding 0 起）")
    print(f"  日志:        {LOG_DIR}")
    print()
    print("采集任务默认 STOPPED，通过 Web Admin 任务页或 API 启动：")
    print(
        f"  curl -X POST -H 'X-API-Key: {write_token}' "
        f"http://127.0.0.1:{WEB_PORT}/api/v1/collector/tasks/sim-modbus-all/start"
    )
    print("停止环境: python3 scripts/acceptance.py stop")


def _cmd_status() -> int:
    if not STATE_PATH.is_file():
        print("验收环境未运行")
        return 1
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    for name, pid in state["processes"].items():
        try:
            os.kill(int(pid), 0)
            alive = "运行中"
        except ProcessLookupError:
            alive = "已退出"
        print(f"{name}: pid={pid} {alive}")
    print(f"写操作 token: {state['write_token']}")
    return 0


def _cmd_stop() -> int:
    if not STATE_PATH.is_file():
        print("验收环境未运行")
        return 1
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    for name, pid in state["processes"].items():
        print(f"停止 {name} (pid={pid})…")
        _kill(int(pid), name)
    if state.get("with_redis"):
        _stop_redis()
    STATE_PATH.unlink(missing_ok=True)
    # 模拟器自身的 pid/state 文件
    simulator_run = REPO_ROOT / "run" / "simulator"
    for path in ("simulator.pid", "state.json", "faults.json"):
        (simulator_run / path).unlink(missing_ok=True)
    print("验收环境已停止")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start_p = sub.add_parser("start", help="拉起完整验收环境")
    start_p.add_argument("--with-redis", action="store_true", help="docker 临时 Redis")
    start_p.add_argument("--with-frontend", action="store_true", help="Vite dev server")
    start_p.add_argument(
        "--foreground",
        action="store_true",
        help="前台驻留（Playwright 等进程管理器托管用）",
    )
    sub.add_parser("status", help="查看运行状态")
    sub.add_parser("stop", help="停止并清理")
    args = parser.parse_args()

    if args.command == "start":
        return _cmd_start(args.with_redis, args.with_frontend, args.foreground)
    if args.command == "status":
        return _cmd_status()
    return _cmd_stop()


if __name__ == "__main__":
    sys.exit(main())
