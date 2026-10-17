"""Playwright 真实后端 E2E 的全栈编排：真实协议从站 + 三个真实进程。

拓扑（全部真实进程，无 mock、无 MSW）::

    Vite dev (15175) ── /api proxy ──▶ Web Server (127.0.0.1:8080)
                                          │ gRPC
                          ┌───────────────┼───────────────┐
                          ▼                               ▼
                    Collector ◀──真实 Modbus/TCP──▶ ModbusMockServer
                    Commander

配置由 tests.support.functional_config 生成（单 Modbus 设备 modbus-1、
telemetry 点组、file sink）。Playwright 的 webServer 以本脚本为命令，
就绪探针打 8080 的 /openapi.json；停止时 SIGTERM 触发优雅清理。
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import signal
import subprocess
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.fixtures.servers.modbus_server import ModbusMockServer  # noqa: E402
from tests.support.functional_config import write_functional_config  # noqa: E402
from tests.support.process import free_port  # noqa: E402
from tests.support.wait import wait_until  # noqa: E402

HTTP_PORT = 8080


async def main() -> int:
    work_dir = Path("/tmp/wind-hub-web-e2e")
    work_dir.mkdir(parents=True, exist_ok=True)
    config_dir = work_dir / "cfg"

    modbus = ModbusMockServer(port=free_port())
    await modbus.start()

    # 每次启动重建配置目录，避免上次残留的 revision/digest 状态。
    import shutil

    if config_dir.exists():
        shutil.rmtree(config_dir)
    write_functional_config(config_dir, modbus.port)

    # 注意：不用 tests.support.process 的 start_*（它们以 start_new_session
    # 让每个服务自成进程组）。Playwright 停止 webServer 时只对脚本所在
    # 进程组发 SIGKILL，独立组的服务会孤儿化并继续占用 8080。这里刻意
    # 同进程组启动，组杀会直接覆盖三个服务；SIGTERM 路径另走 _shutdown。
    collector_port = free_port()
    commander_port = free_port()

    def _spawn(name: str, argv: list[str]) -> subprocess.Popen[str]:
        log_file = (work_dir / f"{name}.log").open("w", encoding="utf-8")
        return subprocess.Popen(  # noqa: S603 - 参数全部内部构造
            argv,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

    collector_p = _spawn(
        f"collector-{collector_port}",
        [
            "wind-hub-collector",
            "--config", str(config_dir),
            "--collector-id", "collector",
            "--grpc-host", "127.0.0.1",
            "--grpc-port", str(collector_port),
        ],
    )
    commander_p = _spawn(
        f"commander-{commander_port}",
        [
            "wind-hub-commander",
            "--config", str(config_dir),
            "--grpc-host", "127.0.0.1",
            "--grpc-port", str(commander_port),
        ],
    )
    server_p = _spawn(
        f"web-server-{HTTP_PORT}",
        [
            "wind-hub-server",
            "--config", str(config_dir),
            "--host", "127.0.0.1",
            "--port", str(HTTP_PORT),
            "--commander", f"127.0.0.1:{commander_port}",
            "--worker-probe-interval", "60",
            "--collector", f"collector=127.0.0.1:{collector_port}",
        ],
    )
    procs = (server_p, commander_p, collector_p)

    stop_event = asyncio.Event()

    def _force_cleanup() -> None:
        for proc in procs:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    atexit.register(_force_cleanup)

    def _request_stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, _request_stop)

    async def _ready() -> bool | None:
        """wait_until 谓词：200 返回 True（就绪），其余/异常返回 None。"""
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                response = await client.get(
                    f"http://127.0.0.1:{HTTP_PORT}/openapi.json"
                )
                return response.status_code == 200 or None
        except httpx.HTTPError:
            return None

    def _stop_one(proc: subprocess.Popen[str]) -> None:
        # 同步阻塞调用放入线程并发停机，避免串行等待超过停机宽限。
        try:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=8.0)
        except Exception:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    async def _shutdown() -> None:
        await asyncio.gather(
            *(asyncio.to_thread(_stop_one, proc) for proc in procs),
            return_exceptions=True,
        )
        await modbus.stop()

    try:
        try:
            # mypy 无法从 bool | None 返回推断 wait_until 的类型参数 T。
            await wait_until(
                _ready,  # type: ignore[arg-type]
                timeout=40.0,
                interval=0.3,
            )
        except BaseException:
            await _shutdown()
            raise
        print(f"REAL BACKEND READY on 127.0.0.1:{HTTP_PORT}", flush=True)
        await stop_event.wait()
    finally:
        await _shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
