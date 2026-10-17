"""E 组：10/50/100 台 ADS 设备的多设备并发规模基线。

与 A-D 组（单设备/单连接）不同，本组每台设备拥有独立的内存服务器、独立
pyads Connection 与独立 ``_cmd_lock``，设备间真正并行——并行度上限来自
**进程级** asyncio 默认 ThreadPoolExecutor（生产配置中无 max_workers
项，CPython 默认 min(32, cpu+4)，本机 24）。

矩阵：设备数 10/50/100 × 池容量 16/32/64/128（测试循环通过
``set_default_executor`` 精确指定，不改生产代码）。

每 cell 阶段并记录：
- connect：全部设备并发连接；
- steady：3 轮全设备 health + read_many(5 点) 并发；
- control：全设备单点写与全设备读同一 gather 内交错；
- 每阶段 P50/P95/P99、成功率、fleet wall、executor 峰值线程/排队延迟、
  进程 RSS/CPU/FD；
- 故障 cell（50 设备/池 32）：单点 reject / disconnect / 1s straggler，
  检验故障隔离、有界性与掉队者对 fleet 墙钟的支配。

原始 JSON 落 ``$WIND_HUB_ADS_BASELINE_DIR``（默认
``artifacts/ads-concurrency-baseline/e_scale``）。标记 slow。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pyads import constants  # type: ignore[import-untyped]

from core.application.errors import ProtocolError
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
from tests.performance.collector import _read_cpu_seconds, _read_fd_count, _read_memory_mb
from tests.support.ads_controlled_server import ControlledAdsMemoryServer
from tests.support.ads_memory_server import pack_points
from tests.support.ads_profiling import (
    LatencyRecorder,
    ProfilingExecutor,
    executor_report,
    summarize,
)
from tests.support.units import NONE_UNIT

_GROUP = 0x4020
_NET_ID = "127.0.0.1.2.1"
_LOCAL_NET_ID = "127.0.0.1.1.1"
_POINTS_PER_DEVICE = 5

_REPO_ROOT = Path(__file__).resolve().parents[5]
_RESULTS_DIR = Path(
    os.environ.get(
        "WIND_HUB_ADS_BASELINE_DIR",
        str(_REPO_ROOT / "artifacts" / "ads-concurrency-baseline" / "e_scale"),
    )
)

pytestmark = [pytest.mark.ads, pytest.mark.real_service, pytest.mark.slow]

_ip_seq = itertools.count(1)


def _device_point(index: int) -> Point:
    return Point(
        point_id=f"p{index}",
        business_point_id=BusinessPointId(f"p{index}"),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"data_type": "REAL", "index_group": _GROUP, "index_offset": index * 4},
    )


class Device:
    def __init__(self, seq: int) -> None:
        del seq
        index = next(_ip_seq)
        # 全 cell 顺序编号（cell 间服务器先停后启，地址可复用）。
        address = f"127.37.{index // 250}.{1 + index % 250}"
        self.server = ControlledAdsMemoryServer(ip_address=address)
        self.address = address
        points = {f"p{i}": _device_point(i) for i in range(_POINTS_PER_DEVICE)}
        self.point_ids = tuple(points)
        # 每台设备必须有唯一 AMS Net ID：AdsLib 路由按 NetID 聚合，多台
        # 设备共用同一 NetID 会在客户端路由层互相覆盖（A10 已实测该现象）。
        # 由 IP 派生 6 段 NetID（127.37.x.y → 127.37.x.y.1.1）。
        target_net_id = f"{address}.1.1"
        self.driver = ADSDriver(
            ConnectionEndpoint(address, 48898),
            PointTable(PointTableId("dev"), Protocol("ads"), points),
            {
                "target_net_id": target_net_id,
                "timeout": 5.0,
                "twincat_version": "3",
            },
        )

    def init_memory(self) -> None:
        self.server.set_values(_GROUP, 0, pack_points([("REAL", 1.25)] * _POINTS_PER_DEVICE))


class Fleet:
    def __init__(self, n_devices: int, pool_size: int) -> None:
        self.n = n_devices
        self.pool = ProfilingExecutor(max_workers=pool_size, name_prefix=f"ads-fleet-{n_devices}")
        self.latency = LatencyRecorder()
        self.devices: list[Device] = []

    async def start_servers(self) -> None:
        # 服务器启动是阻塞线程操作，放到线程避免卡住事件循环。
        def boot() -> None:
            for device in self.devices:
                device.server.start()

        await asyncio.to_thread(boot)
        for device in self.devices:
            device.init_memory()

    async def connect_all(self) -> None:
        async def connect_one(device: Device) -> None:
            with self.latency.time("connect"):
                await device.driver.connect()

        await asyncio.gather(*(connect_one(d) for d in self.devices))

    async def steady_round(self) -> None:
        async def health_one(device: Device) -> None:
            with self.latency.time("health"):
                health = await device.driver.health()
            assert health.healthy

        async def read_one(device: Device) -> None:
            with self.latency.time("read_many_5"):
                samples = await device.driver.read_many(device.point_ids)
            assert len(samples) == _POINTS_PER_DEVICE
            assert all(sample.quality == Quality.GOOD for sample in samples)

        await asyncio.gather(*(task(d) for d in self.devices for task in (health_one, read_one)))

    async def control_round(self) -> int:
        failures = 0

        async def write_one(device: Device, seq: int) -> None:
            nonlocal failures
            try:
                with self.latency.time("write_control"):
                    result = await device.driver.write_one(
                        ProtocolWrite(device.point_ids[-1], float(seq))
                    )
                if result.outcome.name != "CONFIRMED_SUCCESS":
                    failures += 1
            except ProtocolError:
                failures += 1

        async def read_one(device: Device) -> None:
            with self.latency.time("read_control_phase"):
                await device.driver.read_many(device.point_ids)

        await asyncio.gather(
            *(
                coro
                for seq, device in enumerate(self.devices)
                for coro in (write_one(device, seq), read_one(device))
            ),
            return_exceptions=False,
        )
        return failures

    async def close(self) -> None:
        async def close_one(device: Device) -> None:
            with self.latency.time("close"):
                if device.driver.is_open():
                    await device.driver.close()

        await asyncio.gather(*(close_one(d) for d in self.devices))

    def stop_servers(self) -> None:
        for device in self.devices:
            device.server.stop()

    def report(self, cell: str, extra: dict[str, object]) -> dict[str, object]:
        data: dict[str, object] = {
            "cell": cell,
            "devices": self.n,
            "executor": executor_report(self.pool),
            "latency": {
                label: summarize(values) for label, values in sorted(self.latency.samples.items())
            },
        }
        data.update(extra)
        return data


@pytest.fixture(scope="module")
async def local_ams() -> AsyncIterator[None]:
    await ADSDriver.initialize_local(ADSLocalConfig(_LOCAL_NET_ID, "127.0.0.1"))
    try:
        yield
    finally:
        await ADSDriver.close_local()


_SCALES = (10, 50, 100)
_POOLS = (16, 32, 64, 128)


def _save(name: str, report: dict[str, object]) -> None:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (_RESULTS_DIR / f"{name}.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))


@pytest.mark.parametrize("pool_size", _POOLS)
@pytest.mark.parametrize("n_devices", _SCALES)
async def test_e_scale_matrix(n_devices: int, pool_size: int, local_ams: None) -> None:
    """12 cell 健康矩阵：全设备并发 connect/health/read/write。"""
    del local_ams
    loop = asyncio.get_running_loop()
    fleet = Fleet(n_devices, pool_size)
    fleet.devices = [Device(seq) for seq in range(n_devices)]

    rss_start = _read_memory_mb()
    cpu_start = _read_cpu_seconds()
    fds_start = _read_fd_count()
    wall: dict[str, float] = {}
    try:
        # 服务器引导走默认 executor，避免长服务样本污染并发探针；
        # 探针在驱动连接之前安装。
        await fleet.start_servers()
        loop.set_default_executor(fleet.pool)

        t0 = time.monotonic()
        await fleet.connect_all()
        wall["connect_wall_s"] = time.monotonic() - t0

        t0 = time.monotonic()
        for _ in range(3):
            await fleet.steady_round()
        wall["steady_wall_s"] = time.monotonic() - t0

        t0 = time.monotonic()
        write_failures = await fleet.control_round()
        wall["control_wall_s"] = time.monotonic() - t0
        assert write_failures == 0

        cpu_used = _read_cpu_seconds() - cpu_start
        report = fleet.report(
            f"e{n_devices}_p{pool_size}",
            {
                "wall": wall,
                "process": {
                    "rss_mb_start": rss_start,
                    "rss_mb_end": _read_memory_mb(),
                    "cpu_seconds": cpu_used,
                    "fds_start": fds_start,
                    "fds_end": _read_fd_count(),
                    "threads": threading.active_count(),
                },
                "fault": "none",
            },
        )
        _save(f"e{n_devices}_p{pool_size}", report)

        # 硬红线：全成功、无线性失败、fleet 墙钟有界。
        lat = report["latency"]
        assert lat["read_many_5"]["n"] == 3 * n_devices, report
        assert lat["write_control"]["n"] == n_devices, report
        assert lat["health"]["n"] == 3 * n_devices, report
        assert lat["write_control"]["p99"] < 5.0, report
        assert wall["control_wall_s"] < 10.0, report
        # 并行度确实随池容量放开（设备间无共享连接）。
        peak = report["executor"]["peak_active_threads"]
        assert peak <= pool_size
        assert peak >= min(pool_size, n_devices, 4), report
    finally:
        await fleet.close()
        fleet.stop_servers()
        fleet.pool.shutdown(wait=True)


_FAULT_DEVICES = 50
_FAULT_POOL = 32


@pytest.mark.parametrize("fault", ["reject", "disconnect", "straggler"])
async def test_e_fault_isolation_50_devices(fault: str, local_ams: None) -> None:
    """50 设备中单台故障：其余设备不受污染，fleet 行为有界。"""
    del local_ams
    loop = asyncio.get_running_loop()
    fleet = Fleet(_FAULT_DEVICES, _FAULT_POOL)
    fleet.devices = [Device(seq) for seq in range(_FAULT_DEVICES)]
    victim = fleet.devices[0]
    try:
        await fleet.start_servers()
        loop.set_default_executor(fleet.pool)
        await fleet.connect_all()

        control = victim.server.control
        if fault == "reject":
            # 清空受害设备内存：Sum Read 走正常错误路径，帧内逐点 1808、
            # 即时返回 BAD（不是短响应触发的客户端 5s 超时）。
            victim.server.handler.reset_runtime()
        elif fault == "straggler":
            control.set_delay(constants.ADSCOMMAND_READWRITE, 1.0)

        isolated_errors: list[int] = []
        healthy_reads = 0

        async def read_one(device: Device) -> None:
            nonlocal healthy_reads
            if device is victim and fault == "disconnect":
                # 受害设备：连接死亡，允许失败，但必须有界。
                try:
                    await device.driver.read_many(device.point_ids)
                    isolated_errors.append(0)  # 连接尚未感知死亡也可接受
                except ProtocolError:
                    isolated_errors.append(0)
                return
            samples = await device.driver.read_many(device.point_ids)
            if device is victim and fault == "reject":
                # 受害设备：Sum Read 正常返回帧内逐点 1808 → 全 BAD。
                assert all(sample.quality == Quality.BAD for sample in samples), samples
                isolated_errors.append(0)
            else:
                assert all(sample.quality == Quality.GOOD for sample in samples)
                healthy_reads += 1

        if fault == "disconnect":
            # 读进行中杀掉受害设备 TCP。
            victim.server._stop_tcp()  # noqa: SLF001 - 故障注入

        t0 = time.monotonic()
        await asyncio.gather(*(read_one(d) for d in fleet.devices))
        wall = time.monotonic() - t0

        if fault in {"reject", "disconnect"}:
            assert len(isolated_errors) == 1
        # straggler 受害设备只是慢，读仍然 GOOD；reject/disconnect 少一台。
        expected_healthy = _FAULT_DEVICES - (1 if fault in {"reject", "disconnect"} else 0)
        assert healthy_reads == expected_healthy, (fault, healthy_reads)

        report = fleet.report(
            f"e_fault_{fault}",
            {"fault": fault, "fleet_read_wall_s": wall},
        )
        _save(f"e_fault_{fault}", report)

        # straggler：fleet 墙钟必然被单点 1s 延迟支配（无优先级/快路径）。
        if fault == "straggler":
            assert wall >= 0.9, report
        assert wall < 8.0, report
    finally:
        await fleet.close()
        fleet.stop_servers()
        fleet.pool.shutdown(wait=True)
