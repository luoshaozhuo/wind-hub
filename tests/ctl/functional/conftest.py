"""ctl functional 共享环境——真实 Collector 全栈 + 后台事件循环线程。

``wind_hub_ctl.main.main()`` 内部调用 ``asyncio.run``，因此 ctl 的
functional 测试必须是**同步**函数；被控侧（Modbus fixture server、
真实 AssembledRuntime、真实 gRPC Server）运行在独立线程的事件循环上，
通过 ``asyncio.run_coroutine_threadsafe`` 从测试线程驱动。

被控侧与 collector functional 的唯一差别是多了 gRPC 控制面——
这正是 ctl 的耦合点：所有断言都经过 ``main()`` → gRPC → Runtime
的完整链路，不直接触碰 Runtime 内部（唯一例外是测试前置条件的
建立与 fixture server 侧的独立回读）。
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import threading
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import pytest

from tests.collector.functional.conftest import (
    functional_sink_factory,
    write_functional_config,
)
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.system.process import free_port
from wind_hub_collector.adapter.inbound.grpc.server import CollectorGrpcServer, build_grpc_server
from wind_hub_collector.application.runtime.collector_identity import CollectorIdentity
from wind_hub_collector.assembly import AssembledRuntime, assemble, start_runtime, stop_runtime
from wind_hub_ctl.main import main as ctl_main

T = TypeVar("T")


class _LoopThread:
    """托管一个后台事件循环线程。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="ctl-functional", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float = 30.0) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10.0)
        self.loop.close()


@dataclass
class CtlEnvironment:
    """ctl functional 测试的被控 Collector 环境。"""

    target: str
    """gRPC endpoint（``host:port``），即 ``--target`` 的值。"""
    config_dir: Path
    rt: AssembledRuntime
    server: ModbusMockServer
    grpc_server: CollectorGrpcServer
    _loop_thread: _LoopThread

    def run(self, coro: Coroutine[Any, Any, T], timeout: float = 30.0) -> T:
        """在被控侧事件循环上执行协程（建前置条件 / fixture 回读用）。"""
        return self._loop_thread.run(coro, timeout)


@pytest.fixture(scope="module")
def ctl_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CtlEnvironment]:
    """模块级被控 Collector：真实 Runtime + gRPC + Modbus fixture。"""
    tmp = tmp_path_factory.mktemp("ctl-functional")
    modbus_port = free_port()
    grpc_port = free_port()
    config_dir = write_functional_config(tmp / "cfg", modbus_port)

    loop_thread = _LoopThread()
    loop_thread.start()
    server = ModbusMockServer(port=modbus_port)

    async def _start() -> tuple[AssembledRuntime, CollectorGrpcServer]:
        await server.start()
        rt = assemble(config_dir, sink_factory=functional_sink_factory)
        await start_runtime(rt)
        identity = CollectorIdentity(
            collector_id="ctl-functional",
            boot_id="boot-ctl-functional",
            config_hash="hash-ctl-functional",
        )
        grpc_server = build_grpc_server(rt, identity, host="127.0.0.1", port=grpc_port)
        await grpc_server.start()
        return rt, grpc_server

    rt, grpc_server = loop_thread.run(_start())
    env = CtlEnvironment(
        target=grpc_server.endpoint,
        config_dir=config_dir,
        rt=rt,
        server=server,
        grpc_server=grpc_server,
        _loop_thread=loop_thread,
    )
    try:
        yield env
    finally:
        async def _stop() -> None:
            await grpc_server.stop(grace=2.0)
            await stop_runtime(rt, timeout=10.0)
            await server.stop()

        try:
            loop_thread.run(_stop())
        finally:
            loop_thread.stop()


@dataclass
class CtlCliResult:
    """一次 ctl CLI 调用的完整结果。"""

    returncode: int
    payload: Any
    """stdout 的 JSON 解析结果；无输出时为 None。"""
    stdout: str
    stderr: str


@pytest.fixture
def run_ctl(
    ctl_env: CtlEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Callable[..., CtlCliResult]:
    """同步调用 ``wind-hub-ctl`` CLI 并结构化返回结果。"""

    def _run(
        *args: str,
        rpc_timeout: float = 10.0,
        target: str | None = None,
    ) -> CtlCliResult:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "wind-hub-ctl",
                "--target",
                target or ctl_env.target,
                "--rpc-timeout",
                str(rpc_timeout),
                *args,
            ],
        )
        returncode = ctl_main()
        captured = capsys.readouterr()
        payload = json.loads(captured.out) if captured.out.strip() else None
        return CtlCliResult(
            returncode=returncode,
            payload=payload,
            stdout=captured.out,
            stderr=captured.err,
        )

    return _run


def decode_float32(registers: list[int]) -> float:
    """按 fixture 的 big-endian 双寄存器布局解码 float32。"""
    hi, lo = registers
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]


INSTANCE_ID = "modbus-telemetry:modbus-1"
