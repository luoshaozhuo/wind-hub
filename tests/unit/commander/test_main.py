"""Commander 进程入口停机硬超时单元测试。"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import commander.main as main_module
from commander.main import build_parser, run_commander


class _HungStopApp:
    """stop 永久挂起的 App 替身——模拟协议关闭卡死。"""

    def __init__(self) -> None:
        self.runtime = SimpleNamespace(devices={})
        self.config_hash = "deadbeefcafe"
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        await asyncio.Event().wait()


class _FakeGrpcServer:
    endpoint = "127.0.0.1:0"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


async def test_run_commander_hard_timeout_forces_exit_past_hung_stop(monkeypatch, tmp_path):
    """优雅停机超过硬超时不拖死进程：记录错误后照常返回。"""
    monkeypatch.setattr(main_module, "assemble_commander", lambda *_a, **_k: _HungStopApp())
    monkeypatch.setattr(
        main_module, "build_grpc_server", lambda *_a, **_k: _FakeGrpcServer()
    )

    def _fake_install_signal_handlers(shutdown_event: asyncio.Event) -> None:
        asyncio.get_running_loop().call_later(0.05, shutdown_event.set)

    monkeypatch.setattr(
        main_module, "_install_signal_handlers", _fake_install_signal_handlers
    )

    started = time.monotonic()
    rc = await run_commander(tmp_path, shutdown_timeout=0.2)
    elapsed = time.monotonic() - started

    assert rc == 0
    assert elapsed < 2.0  # 挂起的 stop 被硬超时截断，而非无限等待


def test_commander_parser_exposes_shutdown_timeout():
    args = build_parser().parse_args(["--config", "/tmp/x", "--shutdown-timeout", "5"])
    assert args.shutdown_timeout == 5.0
    assert build_parser().parse_args(["--config", "/tmp/x"]).shutdown_timeout == 30.0
