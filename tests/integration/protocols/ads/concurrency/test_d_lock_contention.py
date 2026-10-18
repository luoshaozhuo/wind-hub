"""D 组：锁竞争与控制延迟的**定量分解**（真实协议栈）。

回答任务书第四组问题：Driver ``_lock``（asyncio）与 Session ``_cmd_lock``
（threading）在并发负载下各自造成多少等待？批量读取是否拖慢控制写？

测量手段全部在测试侧（不修改生产代码）：

- ``AsyncLockProfiler`` 替换 ``driver._lock``：逐次记录等待/持有时长；
- ``AsyncLockProfiler`` 替换 ``session._gate``（Session 异步命令门，
  命令实际排队的位置）：同上；
- ``ThreadLockProfiler`` 替换 ``session._cmd_lock``：同上；
- ``ProfilingExecutor`` 安装为事件循环默认 executor：记录 to_thread
  排队延迟、服务时长与并发线程峰值；
- ``ControlledAdsMemoryServer`` 按命令注入固定延迟放大持锁窗口；
- 端到端延迟由 ``LatencyRecorder`` 按操作标签记录。

原始统计落 ``$WIND_HUB_ADS_BASELINE_DIR``（默认
``artifacts/ads-concurrency-baseline/d_group``），供报告引用。

标记 slow：含注入延迟，不进 Fast Gate。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pyads import constants  # type: ignore[import-untyped]

from core.application.protocol_contract import ProtocolWrite, Quality
from core.application.settings import ADSLocalConfig
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.ads import ADSDriver
from tests.support.ads_controlled_server import (
    ControlledAdsMemoryServer,
    server_telemetry,
)
from tests.support.ads_memory_server import pack_points
from tests.support.ads_profiling import (
    AsyncLockProfiler,
    LatencyRecorder,
    ProfilingExecutor,
    ThreadLockProfiler,
    executor_report,
    lock_report,
    summarize,
)
from tests.support.units import NONE_UNIT

_GROUP = 0x4020
_NET_ID = "127.0.0.1.2.1"
_LOCAL_NET_ID = "127.0.0.1.1.1"

_REPO_ROOT = Path(__file__).resolve().parents[5]
_RESULTS_DIR = Path(
    os.environ.get(
        "WIND_HUB_ADS_BASELINE_DIR",
        str(_REPO_ROOT / "artifacts" / "ads-concurrency-baseline" / "d_group"),
    )
)

pytestmark = [pytest.mark.ads, pytest.mark.real_service, pytest.mark.slow]

_seq = itertools.count(1)


def _point(point_id: str, offset: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"data_type": "REAL", "index_group": _GROUP, "index_offset": offset},
    )


class Rig:
    """装配受控服务器 + 全探针驱动。"""

    def __init__(self, n_points: int, *, max_workers: int = 32, **options: object) -> None:
        index = next(_seq)
        address = f"127.34.{index // 250}.{1 + index % 250}"
        self.server = ControlledAdsMemoryServer(ip_address=address)
        self.n_points = n_points
        points = {f"p{i}": _point(f"p{i}", i * 4) for i in range(n_points)}
        self.point_ids = tuple(points)
        self.driver = ADSDriver(
            ConnectionEndpoint(address, 48898),
            PointTable(PointTableId("d"), Protocol("ads"), points),
            {"target_net_id": _NET_ID, "timeout": 5.0, "twincat_version": "3", **options},
        )
        # 替换必须在任何命令之前（含 connect）。
        self.driver_lock = AsyncLockProfiler()
        self.driver._lock = self.driver_lock  # noqa: SLF001 - 测试侧探针
        self.executor = ProfilingExecutor(max_workers=max_workers)
        self.cmd_lock: ThreadLockProfiler | None = None
        self.gate_lock: AsyncLockProfiler | None = None
        self.latency = LatencyRecorder()

    async def start(self) -> None:
        self.server.start()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(self.executor)
        await self.driver.connect()
        session = self.driver._session  # noqa: SLF001
        assert session is not None
        self.cmd_lock = ThreadLockProfiler()
        session._cmd_lock = self.cmd_lock  # noqa: SLF001 - 测试侧探针
        self.gate_lock = AsyncLockProfiler()
        session._gate = self.gate_lock  # noqa: SLF001 - 测试侧探针

    def init_memory(self) -> None:
        self.server.set_values(_GROUP, 0, pack_points([("REAL", 1.5)] * self.n_points))

    async def stop(self) -> None:
        await self.driver.close()
        self.server.stop()
        self.executor.shutdown(wait=True)

    def timed(self, label: str) -> LatencyRecorder.Timer:
        return self.latency.time(label)

    def report(self, extra: dict[str, object] | None = None) -> dict[str, object]:
        assert self.cmd_lock is not None
        assert self.gate_lock is not None
        control = self.server.control
        peak = max(
            control.peak_active(command)
            for command in (
                constants.ADSCOMMAND_READ,
                constants.ADSCOMMAND_WRITE,
                constants.ADSCOMMAND_READSTATE,
                constants.ADSCOMMAND_READWRITE,
                constants.ADSCOMMAND_ADDDEVICENOTE,
                constants.ADSCOMMAND_DELDEVICENOTE,
            )
        )
        telemetry = server_telemetry(control)
        telemetry["peak_active"] = peak
        data: dict[str, object] = {
            "driver_lock": lock_report(self.driver_lock),
            "gate_lock": lock_report(self.gate_lock),
            "cmd_lock": lock_report(self.cmd_lock),
            "executor": executor_report(self.executor),
            "server": telemetry,
            "latency": {
                label: summarize(values) for label, values in sorted(self.latency.samples.items())
            },
        }
        if extra:
            data.update(extra)
        return data


@pytest.fixture(scope="module")
async def local_ams() -> AsyncIterator[None]:
    await ADSDriver.initialize_local(ADSLocalConfig(_LOCAL_NET_ID, "127.0.0.1"))
    try:
        yield
    finally:
        await ADSDriver.close_local()


def _save(name: str, report: dict[str, object]) -> None:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (_RESULTS_DIR / f"{name}.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))


async def test_d01_uncontended_latency_baseline(local_ams: None) -> None:
    """无竞争串行基线：逐操作端到端延迟 + 锁持有时间分解。"""
    del local_ams
    rig = Rig(1000)
    await rig.start()
    rig.init_memory()
    try:
        for _ in range(300):
            with rig.timed("health"):
                health = await rig.driver.health()
            assert health.healthy

        for size, reps in ((1, 100), (10, 100), (100, 50), (1000, 20)):
            label = f"read_many_{size}"
            ids = rig.point_ids[:size]
            for _ in range(reps):
                with rig.timed(label):
                    samples = await rig.driver.read_many(ids)
                assert len(samples) == size
                assert all(sample.quality == Quality.GOOD for sample in samples)

        for k in range(100):
            with rig.timed("write_one"):
                results = await rig.driver.write_one(ProtocolWrite("p0", float(k)))
            assert results.outcome.name == "CONFIRMED_SUCCESS"

        for size, reps in ((100, 20), (1000, 10)):
            label = f"write_many_{size}"
            ids = rig.point_ids[:size]
            for k in range(reps):
                with rig.timed(label):
                    results = await rig.driver.write_many(
                        [ProtocolWrite(pid, float(k % 7)) for pid in ids]
                    )
                assert all(r.outcome.name == "CONFIRMED_SUCCESS" for r in results)

        report = rig.report({"scenario": "D01-uncontended-baseline"})
        _save("d01_uncontended_baseline", report)

        # 无竞争：Driver 状态锁、Session 命令门与线程锁都不应出现可观等待。
        assert report["driver_lock"]["contended_count"] == 0, report
        assert report["gate_lock"]["contended_count"] == 0, report
        assert report["cmd_lock"]["contended_count"] == 0, report
        # loopback 上的宽松上界，防止性能退化无人知晓。
        assert report["latency"]["health"]["p99"] < 0.05, report
        assert report["latency"]["read_many_1000"]["p99"] < 0.2, report
    finally:
        await rig.stop()


async def test_d02_control_health_waits_behind_batch_reads(local_ams: None) -> None:
    """16 路 read_many(100) 负载（每命令注入 2ms）下，health 的锁等待量化。"""
    del local_ams
    rig = Rig(100, max_workers=32)
    await rig.start()
    rig.init_memory()

    async def health_rounds(label: str, reps: int) -> None:
        for _ in range(reps):
            with rig.timed(label):
                health = await rig.driver.health()
            assert health.healthy

    try:
        # 本地基线：无延迟无竞争。
        await health_rounds("health_baseline", 40)

        # READWRITE(9) 每命令服务端停留 2ms，放大 _lock 持有窗口。
        rig.server.control.set_delay(constants.ADSCOMMAND_READWRITE, 0.002)

        async def reader(seq: int) -> None:
            del seq
            for _ in range(8):
                with rig.timed("read_many_100_contended"):
                    samples = await rig.driver.read_many(rig.point_ids)
                assert all(sample.quality == Quality.GOOD for sample in samples)

        readers = [asyncio.create_task(reader(i)) for i in range(16)]
        health_task = asyncio.create_task(health_rounds("health_contended", 40))
        await asyncio.gather(*readers, health_task)

        report = rig.report({"scenario": "D02-health-vs-batch-read", "delay_ms": 2})
        _save("d02_health_vs_batch_read", report)

        base = report["latency"]["health_baseline"]["p99"]
        contended = report["latency"]["health_contended"]["p99"]
        # 测量结论（不预设架构）：记录膨胀倍数；硬红线是不失败、不重叠。
        report["health_p99_inflation"] = contended / max(base, 1e-9)
        _save("d02_health_vs_batch_read", report)
        assert contended < 2.0, report  # 宽松有界：单批 100 点 + 2ms
        # 服务端同步调用重叠峰值必须为 1——串行化在真实负载下成立。
        assert report["server"]["peak_active"] == 1, report
        # 排队位置已从 Driver 状态锁移到 Session 命令门：状态锁不再被网络
        # 往返持有，不应观察到竞争（优化后新增的回归红线）。
        assert report["driver_lock"]["contended_count"] == 0, report
        # 命令门外的线程锁只做取消/关闭兜底，不应观察到竞争等待。
        assert report["cmd_lock"]["max_wait"] < 0.005, report
    finally:
        await rig.stop()


async def test_d03_control_write_under_chunked_read_storm(local_ams: None) -> None:
    """8 路 1000 点（2 chunk）Sum Read 风暴中，单点控制写延迟与成功率。"""
    del local_ams
    rig = Rig(1000, max_workers=64)
    await rig.start()
    rig.init_memory()
    write_errors: list[BaseException] = []

    async def reader() -> None:
        for _ in range(5):
            with rig.timed("read_many_1000_storm"):
                samples = await rig.driver.read_many(rig.point_ids)
            assert len(samples) == 1000
            assert all(sample.quality == Quality.GOOD for sample in samples)

    async def controller() -> None:
        for k in range(50):
            try:
                with rig.timed("write_one_control"):
                    result = await rig.driver.write_one(ProtocolWrite("p1", float(k)))
                assert result.outcome.name == "CONFIRMED_SUCCESS"
            except BaseException as exc:  # noqa: BLE001 - 任何失败都是证据
                write_errors.append(exc)

    try:
        await asyncio.gather(*(reader() for _ in range(8)), controller())
        report = rig.report({"scenario": "D03-control-write-vs-read-storm"})
        _save("d03_control_write_vs_read_storm", report)
        assert write_errors == []
        assert report["server"]["peak_active"] == 1, report
        # 控制写必须有界（1000 点读占锁约毫秒级 × 排队，给 2s 宽限）。
        assert report["latency"]["write_one_control"]["p99"] < 2.0, report
    finally:
        await rig.stop()


async def test_d04_chunk_count_scales_lock_hold(local_ams: None) -> None:
    """同一 1000 点写：2 chunk vs 10 chunk，Session 命令门总持有时间随 chunk 数增长。

    优化后 Driver 状态锁不再覆盖网络往返；分块是命令门的独立调度单元，
    因此缩放关系改在 ``gate_lock`` 上验证（总持有时间），同时确认状态锁
    持有时间与 chunk 数脱钩。
    """
    del local_ams
    reports: dict[str, object] = {}

    for label, chunk_size in (("2_chunks", 500), ("10_chunks", 100)):
        rig = Rig(1000, max_subs_per_sum=chunk_size)
        await rig.start()
        rig.init_memory()
        ids = rig.point_ids
        try:
            for k in range(12):
                with rig.timed(f"write_many_1000_{label}"):
                    results = await rig.driver.write_many(
                        [ProtocolWrite(pid, float(1000 + k)) for pid in ids]
                    )
                assert all(r.outcome.name == "CONFIRMED_SUCCESS" for r in results)
            data = rig.report({"scenario": f"D04-{label}", "chunk_size": chunk_size})
            reports[label] = data
        finally:
            await rig.stop()

    held_2 = reports["2_chunks"]["gate_lock"]["held"]["sum"]  # type: ignore[index]
    held_10 = reports["10_chunks"]["gate_lock"]["held"]["sum"]  # type: ignore[index]
    reports["gate_held_ratio_10_over_2"] = held_10 / max(held_2, 1e-9)
    _save("d04_chunk_hold_scaling", reports)
    # 10 个串行分块的命令门总持有时间必须单调高于 2 分块（往返次数线性
    # 增加）；具体比值作为测量结论落盘。
    assert held_10 > held_2, reports
    # Driver 状态锁不再覆盖分块往返：两台 rig 的状态锁总持有时间都应在
    # 毫秒以下，且不再随 chunk 数线性放大（宽松红线，防回归）。
    state_2 = reports["2_chunks"]["driver_lock"]["held"]["sum"]  # type: ignore[index]
    state_10 = reports["10_chunks"]["driver_lock"]["held"]["sum"]  # type: ignore[index]
    assert state_2 < 0.05 and state_10 < 0.05, reports
