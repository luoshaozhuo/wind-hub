"""Reliability：Modbus 链路网络降级（延迟/抖动/丢包/黑洞/短抖）行为验证。

与 recovery 套件「停服务」式的故障不同，这里经 tc netem 在真实穿越
veth pair 的链路上注入**连续谱**的网络损伤（拓扑与原因见 conftest）：

- 延迟/抖动/低丢包是现场基站链路的常态——采集必须无感穿过，绝不
  允许误判断连（假断连会触发无意义的重连与实例重建）；
- 黑洞（100% 丢包）必须被读超时检测为断连、进入退避重连，恢复后
  数据面续上；
- 短抖（亚超时闪断）不得重建连接；超过读超时的中断必须重连恢复。

观测点：ctl status 的 ``devices_connected``、metrics 的
``device_reconnects`` / ``device_connect_failures``、输出文件行数。

需要 root（无权限时整套 skip，报告计 NOT_RUN）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from tests.performance.netem import NetemScenario
from tests.reliability.network.conftest import NetemLink
from tests.reliability.recovery.helpers import ctl_status, telemetry_task, wait_status
from tests.support.config_helper import write_config_tree
from tests.support.control import apply_placement_and_start_instance, metrics_snapshot
from tests.support.process import CollectorProcess
from tests.support.wait import read_csv, wait_file_rows

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]

TASK_ID = "modbus-telemetry"
INSTANCE_ID = "modbus-telemetry:modbus-1"
DEVICE_ID = "modbus-1"

#: 采集周期 0.2s、读超时 2.0s——延迟/抖动/丢包断言的判定基准。
READ_TIMEOUT_S = 2.0


def _write_config(base: Path, link: NetemLink, sink_path: Path) -> Path:
    from tests.support.functional_config import MODBUS_POINTS, modbus_device_dict

    device = modbus_device_dict(link.device_port)
    device["endpoint"]["host"] = link.device_host
    return write_config_tree(
        base,
        devices=[device],
        point_tables={"modbus": {"points": list(MODBUS_POINTS)}},
        sinks=[
            {
                "name": "file_sink",
                "type": "file",
                "connection": {
                    "path": str(sink_path),
                },
            }
        ],
        tasks=[telemetry_task("file_sink")],
        system={"runtime": {"connect_timeout": 1.0, "read_timeout": READ_TIMEOUT_S}},
    )


async def _start_stack(
    collector_factory, link: NetemLink, tmp_path: Path
) -> tuple[CollectorProcess, Path]:
    """起 Collector 并确认故障前基线：设备已连接、数据在出。"""
    sink_path = tmp_path / "out" / "telemetry.csv"
    config_dir = _write_config(tmp_path / "cfg", link, sink_path)
    proc: CollectorProcess = await collector_factory(config_dir)
    await apply_placement_and_start_instance(
        proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID
    )
    await wait_status(
        proc,
        lambda p: p["devices_connected"] == 1,
        timeout=20.0,
        description="device connected over veth link",
    )
    await wait_file_rows(sink_path, min_rows=2)
    return proc, sink_path


async def _reconnects(proc: CollectorProcess) -> int:
    snap = await metrics_snapshot(proc.grpc_target)
    return snap["device_reconnects"].get(DEVICE_ID, 0)


async def _assert_link_healthy(
    proc: CollectorProcess, sink_path: Path, duration: float
) -> None:
    """在损伤窗口内持续采样：设备必须始终保持已连接、数据持续在出。

    采样是严格断言（不是 wait_until）——任何一次 ``devices_connected``
    掉 0 都是假断连，测试失败。
    """
    rows_before = len(read_csv(sink_path))
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        status = await ctl_status(proc)
        assert status is not None, "本地控制面 status 查询失败（不应受链路损伤影响）"
        assert status["devices_connected"] == 1, (
            f"链路损伤下出现假断连: {status.get('devices_connected')}"
        )
        await asyncio.sleep(0.5)
    await wait_file_rows(sink_path, min_rows=rows_before + 2, timeout=5.0)


class TestDelayAndJitter:
    async def test_delay_ladder_no_false_disconnect(
        self, netem_link: NetemLink, collector_factory, tmp_path: Path
    ) -> None:
        """NET-01：10ms→50ms 延迟阶梯——无假断连、零重连、数据连续。"""
        proc, sink_path = await _start_stack(collector_factory, netem_link, tmp_path)
        reconnects_base = await _reconnects(proc)

        for delay_ms in (10, 50):
            netem_link.apply(NetemScenario(name=f"delay_{delay_ms}ms", delay_ms=delay_ms))
            try:
                await _assert_link_healthy(proc, sink_path, duration=3.0)
            finally:
                netem_link.clear()

        assert await _reconnects(proc) == reconnects_base, (
            "纯延迟不得触发任何重连（延迟不是断连）"
        )
        await _assert_link_healthy(proc, sink_path, duration=1.0)

    async def test_jitter_no_reconnect_storm(
        self, netem_link: NetemLink, collector_factory, tmp_path: Path
    ) -> None:
        """NET-02：20ms±10ms 抖动——健康状态稳定，无重连风暴。"""
        proc, sink_path = await _start_stack(collector_factory, netem_link, tmp_path)
        reconnects_base = await _reconnects(proc)

        netem_link.apply(
            NetemScenario(name="jitter_20ms_10ms", delay_ms=20, jitter_ms=10)
        )
        try:
            await _assert_link_healthy(proc, sink_path, duration=4.0)
        finally:
            netem_link.clear()

        assert await _reconnects(proc) == reconnects_base


class TestPacketLoss:
    async def test_low_loss_tolerated(
        self, netem_link: NetemLink, collector_factory, tmp_path: Path
    ) -> None:
        """NET-03：1% 丢包零影响（TCP 重传吸收）；10% 丢包可容忍且恢复。

        1% 丢包下任何断连/重连都是健康度判定过敏——必须暴露。10% 丢包
        下允许极个别读超时（请求方向 TCP 重传预算内 4 次传输全丢概率
        ~1e-4/读），但绝不允许重连风暴，且清除后必须恢复。
        """
        proc, sink_path = await _start_stack(collector_factory, netem_link, tmp_path)
        reconnects_base = await _reconnects(proc)

        netem_link.apply(NetemScenario(name="loss_1pct", loss_pct=1))
        try:
            await _assert_link_healthy(proc, sink_path, duration=5.0)
        finally:
            netem_link.clear()
        assert await _reconnects(proc) == reconnects_base, (
            "1% 丢包不得触发重连——健康度判定对丢包过敏"
        )

        netem_link.apply(NetemScenario(name="loss_10pct", loss_pct=10))
        try:
            await asyncio.sleep(5.0)
        finally:
            netem_link.clear()
        assert (await _reconnects(proc)) - reconnects_base <= 1, (
            "10% 丢包下出现重连风暴"
        )
        await wait_status(
            proc,
            lambda p: p["devices_connected"] == 1,
            timeout=15.0,
            description="device connected after 10% loss cleared",
        )
        await _assert_link_healthy(proc, sink_path, duration=1.0)

    async def test_blackhole_detected_and_recovers(
        self, netem_link: NetemLink, collector_factory, tmp_path: Path
    ) -> None:
        """NET-04：黑洞（100% 丢包）→ 读超时判断连 → 退避重连 → 清除后恢复。

        与「停 modbus 服务」不同：黑洞下 TCP 连接本身不死，只能靠读
        超时检测——这是现场基站掉线的真实形态。
        """
        proc, sink_path = await _start_stack(collector_factory, netem_link, tmp_path)
        reconnects_base = await _reconnects(proc)

        netem_link.apply(NetemScenario(name="blackhole", loss_pct=100))
        try:
            await wait_status(
                proc,
                lambda p: p["devices_connected"] == 0,
                timeout=20.0,
                description="blackhole detected as disconnect",
            )

            # 黑洞持续 6s：connect 尝试必须被退避节流（≈4 次，远少于
            # 采集 tick 的 30 次）。
            first = await metrics_snapshot(proc.grpc_target)
            await asyncio.sleep(6.0)
            last = await metrics_snapshot(proc.grpc_target)
            attempts = (
                last["device_connect_failures"].get(DEVICE_ID, 0)
                - first["device_connect_failures"].get(DEVICE_ID, 0)
            )
            runs = (
                last["counters"]["acquisition_runs"]
                - first["counters"]["acquisition_runs"]
            )
            assert 1 <= attempts <= 6, f"黑洞期 connect 尝试未被退避节流: {attempts}"
            assert attempts * 5 < runs
        finally:
            netem_link.clear()

        await wait_status(
            proc,
            lambda p: p["devices_connected"] == 1,
            timeout=20.0,
            description="reconnected after blackhole cleared",
        )
        assert await _reconnects(proc) > reconnects_base
        await _assert_link_healthy(proc, sink_path, duration=1.0)


class TestShortFlap:
    async def test_sub_timeout_flap_does_not_reconnect(
        self, netem_link: NetemLink, collector_factory, tmp_path: Path
    ) -> None:
        """NET-05a：500ms 闪断（< 读超时 2s）——TCP 重传吸收，不得重建连接。"""
        proc, sink_path = await _start_stack(collector_factory, netem_link, tmp_path)
        reconnects_base = await _reconnects(proc)

        await netem_link.outage(0.5)

        await _assert_link_healthy(proc, sink_path, duration=1.5)
        assert await _reconnects(proc) == reconnects_base, (
            "500ms 闪断不得触发连接重建（亚超时中断应由 TCP 重传吸收）"
        )

    async def test_timeout_boundary_flap_recovers(
        self, netem_link: NetemLink, collector_factory, tmp_path: Path
    ) -> None:
        """NET-05b：2s 闪断（≈读超时）——允许一次断连重连，但必须自动恢复。"""
        proc, sink_path = await _start_stack(collector_factory, netem_link, tmp_path)

        await netem_link.outage(2.0)

        await wait_status(
            proc,
            lambda p: p["devices_connected"] == 1,
            timeout=15.0,
            description="link healthy after 2s flap",
        )
        await _assert_link_healthy(proc, sink_path, duration=1.0)

    async def test_supra_timeout_flap_reconnects(
        self, netem_link: NetemLink, collector_factory, tmp_path: Path
    ) -> None:
        """NET-05c：5s 闪断（> 读超时）——必须判断连、退避、恢复后重连出数。"""
        proc, sink_path = await _start_stack(collector_factory, netem_link, tmp_path)
        reconnects_base = await _reconnects(proc)

        outage = asyncio.create_task(netem_link.outage(5.0))
        try:
            await wait_status(
                proc,
                lambda p: p["devices_connected"] == 0,
                timeout=15.0,
                description="5s flap detected as disconnect",
            )
        finally:
            await outage

        await wait_status(
            proc,
            lambda p: p["devices_connected"] == 1,
            timeout=20.0,
            description="reconnected after 5s flap",
        )
        assert await _reconnects(proc) > reconnects_base
        await _assert_link_healthy(proc, sink_path, duration=1.0)
