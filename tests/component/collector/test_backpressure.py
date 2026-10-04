"""Component：慢 Sink 背压与快慢 Sink 隔离。

链路级验证（真实 Runtime/Engine/Dispatcher/asyncio.Queue + 真实 Modbus
fixture）：SlowSink 写入人为放慢制造 queue 积压，断言：

- queue 深度始终不超过 ``queue_maxsize``（内存有界）；
- ``points_dropped`` 按配置策略（drop_old / drop_new）正确计数；
- 快 Sink（NullSink）数据流不被慢 Sink 的积压拖慢；
- Runtime 不崩、采集不停。

SlowSink 只存在于测试侧 sink_factory，不进入生产装配。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.component.collector.conftest import MODBUS_POINTS, modbus_device_dict
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.fixtures.sinks.null_sink import NullSink
from tests.fixtures.sinks.slow_sink import SlowSink
from tests.support.config_helper import write_config_tree
from tests.support.process import free_port
from tests.support.wait import wait_until
from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.assembly import AssembledRuntime, assemble, start_runtime, stop_runtime
from wind_hub_core.config.sinks import ResolvedSinkConfig
from wind_hub_core.model.errors import ConfigError

INSTANCE_ID = "modbus-telemetry:modbus-1"

QUEUE_MAXSIZE = 8
#: 采集周期 0.05s（20 tick/s）vs SlowSink 每批 0.4s：queue 必然打满。
FAST_INTERVAL = 0.05
SLOW_DELAY = 0.4


async def _start_bp_runtime(
    tmp_path: Path,
    *,
    backpressure_policy: str,
) -> tuple[AssembledRuntime, ModbusMockServer, NullSink, SlowSink]:
    """装配 fast(null) + slow 双 Sink 的背压运行时。"""
    port = free_port()
    server = ModbusMockServer(port=port)
    await server.start()

    fast = NullSink()
    slow = SlowSink(delay=SLOW_DELAY)

    def _factory(cfg: ResolvedSinkConfig) -> SinkPort:
        if cfg.name == "fast_sink":
            return fast
        if cfg.name == "slow_sink":
            return slow
        raise ConfigError(f"unknown sink '{cfg.name}' (backpressure test factory)")

    config_dir = write_config_tree(
        tmp_path / "cfg",
        devices=[modbus_device_dict(port)],
        point_tables={"modbus": {"points": list(MODBUS_POINTS)}},
        sinks=[
            {
                "name": "fast_sink",
                "type": "file",
                "connection": {"path": str(tmp_path / "fast.jsonl")},
            },
            {
                "name": "slow_sink",
                "type": "file",
                "connection": {"path": str(tmp_path / "slow.jsonl")},
            },
        ],
        tasks=[
            {
                "task_id": "modbus-telemetry",
                "device": "modbus-1",
                "point_group": "telemetry",
                "interval": FAST_INTERVAL,
                "targets": [{"sink": "fast_sink"}, {"sink": "slow_sink"}],
            }
        ],
        system={
            "runtime": {
                "connect_timeout": 2.0,
                "read_timeout": 2.0,
                "write_timeout": 2.0,
                "shutdown_timeout": 5.0,
                "queue_maxsize": QUEUE_MAXSIZE,
                "backpressure_policy": backpressure_policy,
            }
        },
    )
    rt = assemble(config_dir, sink_factory=_factory)
    await start_runtime(rt)
    await rt.tasks.start_instance(INSTANCE_ID)
    return rt, server, fast, slow


async def _stop(rt: AssembledRuntime, server: ModbusMockServer) -> None:
    await stop_runtime(rt)
    await server.stop()


class TestBackpressure:
    async def test_drop_old_bounds_queue_and_isolates_fast_sink(
        self, tmp_path: Path
    ) -> None:
        """SINK-03/04（drop_old）：queue 打满丢弃旧批、计数正确、快 Sink 不堵。"""
        rt, server, fast, slow = await _start_bp_runtime(
            tmp_path, backpressure_policy="drop_old"
        )
        try:
            # ---- queue 打满并开始丢弃 ----
            async def _dropping() -> int | None:
                status = await rt.query.status()
                return status.points_dropped if status.points_dropped > 0 else None

            await wait_until(_dropping, timeout=15.0, description="points dropped")
            assert slow.completed_writes >= 1, "slow sink never consumed"

            # ---- 积压期间：queue 深度有界、快 Sink 持续增长、采集不停 ----
            collected_before = (await rt.query.status()).points_collected
            fast_before = len(fast.received)
            depth_violations: list[dict[str, int]] = []
            for _ in range(5):
                await asyncio.sleep(0.4)
                depths = rt.runtime.sink_queue_depths()
                if depths.get("slow_sink", 0) > QUEUE_MAXSIZE:
                    depth_violations.append(depths)
            assert not depth_violations, f"queue exceeded maxsize: {depth_violations}"

            collected_after = (await rt.query.status()).points_collected
            assert collected_after > collected_before, "acquisition stalled under backpressure"
            fast_growth = len(fast.received) - fast_before
            assert fast_growth > 10, (
                f"fast sink starved by slow sink backlog: +{fast_growth} points in 2s"
            )

            # ---- 丢弃只增不减且总量与采集规模同量级 ----
            status = await rt.query.status()
            assert 0 < status.points_dropped < status.points_collected
        finally:
            await _stop(rt, server)

    async def test_drop_new_bounds_queue_and_isolates_fast_sink(
        self, tmp_path: Path
    ) -> None:
        """SINK-04（drop_new）：queue 打满丢弃新批，快 Sink 同样不受拖累。"""
        rt, server, fast, slow = await _start_bp_runtime(
            tmp_path, backpressure_policy="drop_new"
        )
        try:

            async def _dropping() -> int | None:
                status = await rt.query.status()
                return status.points_dropped if status.points_dropped > 0 else None

            await wait_until(_dropping, timeout=15.0, description="points dropped")

            fast_before = len(fast.received)
            for _ in range(4):
                await asyncio.sleep(0.4)
                depths = rt.runtime.sink_queue_depths()
                assert depths.get("slow_sink", 0) <= QUEUE_MAXSIZE, (
                    f"queue exceeded maxsize: {depths}"
                )
            assert len(fast.received) - fast_before > 10, (
                "fast sink starved under drop_new backpressure"
            )
            assert (await rt.query.status()).points_dropped > 0
        finally:
            await _stop(rt, server)
