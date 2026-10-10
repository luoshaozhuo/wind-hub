"""目标负载验收（spec §13）：各负载形态在真实采集引擎上的短窗口验收。

每个用例跑一个 :data:`tests.soak.runner.PROFILES` 负载形态（真实 Modbus
server、真实引擎、真实或计量 sink），断言：

- 实际采样间隔贴近目标、抖动有界、错过周期率达标；
- 数据完整性：背压零丢弃、sink 侧零丢失零重复（从系统边界核验，
  不信 sink 自报）；
- 重连风暴下驱动反复断连后能恢复且数据继续流动。

路径自动打 ``soak``；本文件额外打 ``performance`` 与 ``modbus``，使
``pytest -m performance`` / ``pytest -m modbus`` 同样收集到这些验收用例。
netem 用例需要 root（CAP_NET_ADMIN），无 root 明确 skip。
"""

from __future__ import annotations

import logging

import pytest

from tests.performance.netem import NetemScenario
from tests.performance.veth import VethManager
from tests.reliability.soak.metrics import CycleStats, SoakMetrics
from tests.reliability.soak.runner import PROFILES, run_soak

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.performance,
    pytest.mark.modbus,
    pytest.mark.real_service,
]

# 错过周期率上限（错过 = 实际间隔 > 1.5 × 目标间隔）。
_MAX_MISSED_RATIO = 0.02
# 平均间隔相对目标的容忍偏差（开发机调度噪声下保持稳健）。
_MAX_MEAN_DEVIATION = 0.25


def _cycles_by_task(metrics: SoakMetrics) -> dict[str, CycleStats]:
    return {c.task_id: c for c in metrics.cycles}


def _assert_healthy_flow(metrics: SoakMetrics, *, min_throughput_ratio: float) -> None:
    """数据完整性公共断言：吞吐达标、零背压丢弃、sink 侧零丢失零重复。"""
    profile = PROFILES[metrics.profile]
    assert metrics.throughput_pps >= profile.expected_pps * min_throughput_ratio, (
        f"吞吐 {metrics.throughput_pps:.1f} 点/s 低于预期 "
        f"{profile.expected_pps:.0f} × {min_throughput_ratio}"
    )
    assert metrics.points_dropped == 0, f"背压丢弃 {metrics.points_dropped} 点"
    # sink 收到数允许少量尾部差（停引擎时队列中未排空的部分），不允许丢失超 1%。
    assert metrics.sink_received >= metrics.points_routed * 0.99, (
        f"sink 收到 {metrics.sink_received} < 路由 {metrics.points_routed} 的 99%"
    )
    assert metrics.sink_duplicates == 0, f"sink 收到 {metrics.sink_duplicates} 个重复点"


def _assert_cadence(metrics: SoakMetrics) -> None:
    """节拍公共断言：每个任务的平均间隔贴近目标、错过周期率达标。"""
    for c in metrics.cycles:
        assert c.cycles > 0, f"任务 {c.task_id} 在测量窗口内没有完成任何采集周期"
        deviation = abs(c.interval_mean_s - c.target_interval_s) / c.target_interval_s
        assert deviation <= _MAX_MEAN_DEVIATION, (
            f"任务 {c.task_id} 平均间隔 {c.interval_mean_s:.3f}s "
            f"偏离目标 {c.target_interval_s:g}s 超过 {_MAX_MEAN_DEVIATION:.0%}"
        )
        missed_ratio = c.missed_cycles / c.cycles
        assert missed_ratio <= _MAX_MISSED_RATIO, (
            f"任务 {c.task_id} 错过周期率 {missed_ratio:.2%} "
            f"（{c.missed_cycles}/{c.cycles}）超过 {_MAX_MISSED_RATIO:.0%}"
        )


class TestTargetLoad:
    """spec §13 目标负载：100 点 ×10Hz + 500 点 ×1Hz。"""

    async def test_target_100x10hz_500x1hz(self) -> None:
        metrics = await run_soak(
            PROFILES["target_100x10hz_500x1hz"], duration_s=20.0, warmup_s=4.0
        )
        cycles = _cycles_by_task(metrics)
        assert set(cycles) == {"fast-000", "slow-000"}

        _assert_cadence(metrics)
        _assert_healthy_flow(metrics, min_throughput_ratio=0.95)

        # 10Hz 任务的节拍精度单独卡严一档：P99 间隔不应超过目标 2 倍。
        fast = cycles["fast-000"]
        assert fast.interval_p99_s <= 0.2, (
            f"10Hz 任务 P99 间隔 {fast.interval_p99_s:.3f}s 超过目标 2 倍"
        )

    async def test_multi_device_200(self) -> None:
        """200 台设备规模负载：全部任务都在节拍上、总吞吐达标。"""
        metrics = await run_soak(
            PROFILES["multi_device_200"], duration_s=15.0, warmup_s=8.0
        )
        assert len(metrics.cycles) == 200
        _assert_cadence(metrics)
        _assert_healthy_flow(metrics, min_throughput_ratio=0.90)

    async def test_mixed_read_write(self) -> None:
        """混合读写：采集节拍不受写命令干扰，写命令全部成功。"""
        metrics = await run_soak(
            PROFILES["mixed_read_write"], duration_s=15.0, warmup_s=4.0
        )
        _assert_cadence(metrics)
        _assert_healthy_flow(metrics, min_throughput_ratio=0.90)

        expected_writes = 15.0 / 0.2  # duration / write_interval_s
        assert metrics.write_commands >= expected_writes * 0.8, (
            f"写命令数 {metrics.write_commands} 低于预期 {expected_writes:.0f} 的 80%"
        )
        assert metrics.write_failures == 0, (
            f"{metrics.write_failures}/{metrics.write_commands} 条写命令失败"
        )


class TestReconnectStorm:
    """重连风暴：协议 server 反复停起，驱动必须反复重连且数据持续流动。"""

    async def test_reconnect_storm(self) -> None:
        profile = PROFILES["reconnect_storm"]
        assert profile.storm is not None
        # 测量窗口须覆盖整个风暴（5 × (4s 间隔 + 2s 停服) = 30s）+ 尾部观察。
        metrics = await run_soak(profile, duration_s=38.0, warmup_s=4.0)

        assert metrics.reconnect_count >= profile.storm.count, (
            f"只观测到 {metrics.reconnect_count} 次重连，"
            f"风暴停起 {profile.storm.count} 次"
        )
        # 风暴期间占空比约 67%（4s 正常 / (4+2)s），吞吐不得跌破一半太多；
        # 关键是风暴后数据仍在流动（周期数远超风暴前的基线部分）。
        _assert_healthy_flow(metrics, min_throughput_ratio=0.40)
        cycles = _cycles_by_task(metrics)["storm-000"]
        expected_if_never_down = 38.0 / 0.2
        assert cycles.cycles >= expected_if_never_down * 0.4, (
            f"周期数 {cycles.cycles} 过低——风暴后采集未恢复？"
        )
        assert metrics.sink_duplicates == 0


class TestNetemScenarios:
    """网络损伤下的负载行为（需 root；veth pair + tc netem 双方向注入）。"""

    @pytest.fixture
    def veth(self):
        manager = VethManager()
        if not manager.check_permission():
            pytest.skip("SKIPPED: netem 场景需要 root（CAP_NET_ADMIN）创建 veth pair")
        pair = manager.create()
        try:
            yield pair
        finally:
            manager.destroy()

    async def test_network_latency(self, veth) -> None:
        """50ms 单向延迟（RTT +100ms）：采集放慢但不错失、不丢点。"""
        metrics = await run_soak(
            PROFILES["netem_base"],
            host=veth.ip_server,
            duration_s=15.0,
            warmup_s=4.0,
            netem_devices=[veth.name_client, veth.name_server],
            netem_scenario=NetemScenario(name="delay_50ms", delay_ms=50),
        )
        _assert_healthy_flow(metrics, min_throughput_ratio=0.50)
        cycles = _cycles_by_task(metrics)["base-000"]
        assert cycles.cycles >= (15.0 / 0.2) * 0.4, (
            f"延迟下周期数 {cycles.cycles} 过低"
        )

    async def test_packet_loss(self, veth) -> None:
        """1% 丢包：TCP 重传兜底，数据完整（零丢弃零重复）、吞吐略降。"""
        metrics = await run_soak(
            PROFILES["netem_base"],
            host=veth.ip_server,
            duration_s=15.0,
            warmup_s=4.0,
            netem_devices=[veth.name_client, veth.name_server],
            netem_scenario=NetemScenario(name="loss_1pct", loss_pct=1),
        )
        _assert_healthy_flow(metrics, min_throughput_ratio=0.40)
