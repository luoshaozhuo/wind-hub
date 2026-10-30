"""CLI 验收测试共享夹具：真实 YAML 配置副本 + 内存 Fake Driver。

所有协议交互均使用 FakeDriver，不触碰真实设备；网络类测试只使用本机
loopback 上的临时 TCP 服务。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from core.application.protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    WriteOutcome,
)
from core.domain.sample_value import Quality

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "configs"
CONFIG_NAMES = ("modbus", "iec104", "ads")


def sample(point_id: str, value: object = 1.0, quality: Quality = Quality.GOOD) -> ProtocolSample:
    return ProtocolSample(
        point_id=point_id,
        value=value,  # type: ignore[arg-type]
        timestamp=datetime.now(UTC),
        quality=quality,
    )


@pytest.fixture
def config_dir(tmp_path: Path) -> Callable[[str], Path]:
    """把仓库示例配置复制到临时目录，测试可任意破坏文件内容。"""

    def _copy(name: str = "modbus") -> Path:
        if name not in CONFIG_NAMES:
            raise KeyError(name)
        target = tmp_path / f"cfg_{name}"
        shutil.copytree(FIXTURE_ROOT / f"example_{name}", target)
        return target

    return _copy


class FakeSubscription:
    """模拟 SUBSCRIBE 句柄：start 推送一批，stop 幂等。"""

    def __init__(self, driver: FakeDriver, callback: Any) -> None:
        self._driver = driver
        self._callback = callback
        self._task: asyncio.Task[None] | None = None
        self.stopped = False

    async def start(self) -> None:
        self._driver.events.append("subscribe_start")

        async def _push() -> None:
            await asyncio.sleep(0)
            await self._callback(tuple(self._driver.sample_points))

        self._task = asyncio.create_task(_push())

    async def stop(self) -> None:
        self._driver.events.append("subscribe_stop")
        if self._task is not None:
            await self._task
        self.stopped = True


class FakeListen:
    """模拟 LISTEN 句柄：建立即推送一批，close 幂等。"""

    def __init__(self, driver: FakeDriver, callback: Any) -> None:
        self._driver = driver
        self._callback = callback
        self.closed = False

    async def serve(self) -> None:
        await asyncio.sleep(0)
        await self._callback(tuple(self._driver.sample_points))

    async def close(self) -> None:
        self._driver.events.append("listen_close")
        self.closed = True


class FakeDriver:
    """记录全部调用的协议 Driver 替身；行为可逐用例配置。"""

    def __init__(
        self,
        device_id: str = "dev",
        *,
        capabilities: frozenset[ProtocolCapability] | None = None,
        healthy: bool = True,
        recorder: list[tuple[str, str]] | None = None,
    ) -> None:
        self.device_id = device_id
        self._recorder = recorder
        self._capabilities = capabilities or frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.READ_MANY,
                ProtocolCapability.WRITE,
                ProtocolCapability.SUBSCRIBE,
                ProtocolCapability.INTERROGATE,
            }
        )
        self.healthy = healthy
        self.health_message: str | None = None
        self.connects = 0
        self.closes = 0
        self.write_calls = 0
        self.read_one_calls = 0
        self.read_many_calls = 0
        self.interrogate_calls = 0
        self.events: list[str] = []
        self.quality: Quality = Quality.GOOD
        self.write_outcome = WriteOutcome.CONFIRMED_SUCCESS
        self.write_timeout = False
        self.read_gate: asyncio.Event | None = None
        self.sample_points = (sample("active_power"), sample("wind_speed", 2.5))
        self.last_write: ProtocolWrite[Any] | None = None

    def _record(self, event: str) -> None:
        self.events.append(event)
        if self._recorder is not None:
            self._recorder.append((self.device_id, event))

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return self._capabilities

    async def connect(self) -> None:
        self.connects += 1
        self._record("connect")

    async def close(self) -> None:
        self.closes += 1
        self._record("close")

    async def health(self) -> ConnectionHealth:
        self._record("health")
        return ConnectionHealth(healthy=self.healthy, message=self.health_message)

    async def read_one(self, point_id: str) -> ProtocolSample:
        self.read_one_calls += 1
        return await self._sample(point_id)

    async def _sample(self, point_id: str) -> ProtocolSample:
        if self.read_gate is not None:
            await self.read_gate.wait()
        return sample(point_id, quality=self.quality)

    async def read_many(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        self.read_many_calls += 1
        # Fake 原生批量读：与 read_one 调用计数独立，便于钉死“不模拟批量”。
        collected: list[ProtocolSample] = []
        for point_id in point_ids:
            collected.append(await self._sample(point_id))
        return tuple(collected)

    async def write_one(self, write: ProtocolWrite[Any]) -> ProtocolWriteResult:
        self.write_calls += 1
        self.last_write = write
        if self.write_timeout:
            await asyncio.Event().wait()
        return ProtocolWriteResult(point_id=write.point, outcome=self.write_outcome, message=None)

    async def interrogate(self, request: Any) -> tuple[ProtocolSample, ...]:
        self.interrogate_calls += 1
        return tuple(self.sample_points)

    def subscribe(self, point_ids: Sequence[str], callback: Any) -> FakeSubscription:
        self.events.append("subscribe_create")
        return FakeSubscription(self, callback)

    async def listen(self, point_ids: Sequence[str], callback: Any) -> FakeListen:
        self.events.append("listen_create")
        handle = FakeListen(self, callback)
        await handle.serve()
        return handle


@pytest.fixture
def fake_drivers(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """在三个 CLI 模块注入同一套按设备 ID 索引的 FakeDriver。"""

    registry: dict[str, Any] = {}
    releases: list[bool] = []
    order: list[tuple[str, str]] = []
    tcp_probes: list[tuple[str, int]] = []
    # 预建示例配置中的设备，用例可在调用前配置其行为。
    registry["modbus-001"] = FakeDriver("modbus-001", recorder=order)
    registry["wtg-101"] = FakeDriver("wtg-101", recorder=order)
    registry["ads-001"] = FakeDriver("ads-001", recorder=order)
    registry["ads-010"] = FakeDriver("ads-010", recorder=order)

    def factory(snapshot: object, device_id: str) -> FakeDriver:
        if device_id not in registry:
            registry[device_id] = FakeDriver(device_id, recorder=order)
        return registry[device_id]

    async def prepare(snapshot: object, driver: FakeDriver) -> bool:
        driver.events.append("prepare_ads")
        return False

    async def release(prepared: bool) -> None:
        releases.append(prepared)

    import cli.pipeline as pipeline_mod
    import cli.protocol_diagnostic as diag_mod
    import cli.protocol_stream as stream_mod

    monkeypatch.setattr(pipeline_mod, "create_driver", factory)
    monkeypatch.setattr(diag_mod, "create_driver", factory)
    monkeypatch.setattr(stream_mod, "create_driver", factory)
    monkeypatch.setattr(pipeline_mod, "prepare_ads", prepare)
    monkeypatch.setattr(diag_mod, "prepare_ads", prepare)
    monkeypatch.setattr(stream_mod, "prepare_ads", prepare)
    monkeypatch.setattr(pipeline_mod, "release_ads", release)
    monkeypatch.setattr(diag_mod, "release_ads", release)
    monkeypatch.setattr(stream_mod, "release_ads", release)

    # Sink 出站探针同样使用替身：CLI 单测是 hermetic 的，不得隐式依赖
    # 宿主机 6379 等端口上恰好有服务（Round 1 曾因此假绿）。
    async def default_fake_tcp(host: str, port: int, *, timeout: float) -> dict[str, Any]:
        tcp_probes.append((host, port))
        return {"status": "PASS", "host": host, "port": port, "elapsed_ms": 0.1}

    monkeypatch.setattr(pipeline_mod, "tcp", default_fake_tcp)
    registry["_releases"] = releases
    registry["_order"] = order
    registry["_tcp_probes"] = tcp_probes
    return registry


@pytest.fixture
def run_cli(capsys: pytest.CaptureFixture[str]) -> Callable[[list[str]], tuple[int, str, str]]:
    """在进程内执行单次 CLI，返回 (退出码, stdout, stderr)。"""

    def _run(argv: list[str]) -> tuple[int, str, str]:
        from cli.main import main

        old_argv = sys.argv
        sys.argv = ["wind-hub", *argv]
        try:
            rc = main()
        except SystemExit as exc:
            rc = int(exc.code) if exc.code is not None else 0
        finally:
            sys.argv = old_argv
        captured = capsys.readouterr()
        return rc, captured.out, captured.err

    return _run
