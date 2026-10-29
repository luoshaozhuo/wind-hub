"""Reliability：Modbus 链路网络降级（延迟/抖动/丢包/黑洞/短抖）行为验证。

与 recovery 套件「停服务」式的故障不同，这里经 tc netem 在真实穿越
veth pair 的链路上注入**连续谱**的网络损伤（拓扑与原因见 conftest）：

- 延迟/抖动/低丢包是现场基站链路的常态——采集必须无感穿过，绝不
  允许误判断连（假断连会触发无意义的重连与实例重建）；
- 黑洞（100% 丢包）必须被读超时检测为断连、进入退避重连，恢复后
  数据面续上；
- 短抖（亚超时闪断）不得重建连接；超过读超时的中断必须重连恢复。

观测点：CLI status 的 ``devices_connected``、metrics 的
``device_reconnects`` / ``device_connect_failures``、Redis 最新值时间戳。

需要 root（无权限时整套 skip，报告计 NOT_RUN）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from tests.support.config_helper import write_config_tree
from tests.support.control import metrics_snapshot, start_task_instance
from tests.support.netem import NetemScenario
from tests.support.process import CollectorProcess
from tests.support.recovery_helpers import (
    TASK_ID,
    cli_status,
    telemetry_task,
    wait_status,
)
from tests.support.redis_client import point_key
from tests.support.wait import wait_redis_value
from tests.system.network.conftest import NetemLink

pytestmark = [
    pytest.mark.reliability,
    pytest.mark.modbus,
    pytest.mark.redis,
    pytest.mark.real_service,
]

INSTANCE_ID = f"{TASK_ID}:modbus-1"
DEVICE_ID = "modbus-1"

#: 采集周期 0.2s、读超时 2.0s——延迟/抖动/丢包断言的判定基准。
READ_TIMEOUT_S = 2.0


def _write_config(base: Path, link: NetemLink, redis_address: str) -> Path:
    from tests.support.functional_config import MODBUS_POINTS, modbus_device_dict

    devices = modbus_device_dict(link.device_port)
    devices[DEVICE_ID]["endpoint"]["host"] = link.device_host
    return write_config_tree(
        base,
        devices=devices,
        point_tables={"modbus": {"points": dict(MODBUS_POINTS)}},
        sinks={
            "redis_sink": {
                "type": "redis",
                "connection": {
                    "host": redis_address.rpartition(":")[0],
                    "port": int(redis_address.rpartition(":")[2]),
                },
                "outputs": {TASK_ID: {"key_prefix": "wind-hub-netem"}},
            }
        },
        tasks=telemetry_task(),
        system={"runtime": {"connect_timeout": 1.0, "read_timeout": READ_TIMEOUT_S}},
    )


async def _start_stack(
    collector_factory, link: NetemLink, tmp_path: Path, redis_address: str
) -> tuple[CollectorProcess, str]:
    """起 Collector 并确认故障前基线：设备已连接、数据在出。"""
    config_dir = _write_config(tmp_path / "cfg", link, redis_address)
    proc: CollectorProcess = await collector_factory(config_dir)
    await start_task_instance(proc.grpc_target, task_id=TASK_ID, instance_id=INSTANCE_ID)
    await wait_status(
        proc,
        lambda p: p["devices_connected"] == 1,
        timeout=20.0,
        description="device connected over veth link",
    )
    await _wait_flow(redis_address)
    return proc, redis_address


async def _reconnects(proc: CollectorProcess) -> int:
    snap = await metrics_snapshot(proc.grpc_target)
    return int(snap["device_reconnects"].get(DEVICE_ID, 0))


async def _wait_flow(redis_address: str, *, after: str | None = None) -> str:
    key = point_key(DEVICE_ID, "rotor_speed", prefix="wind-hub-netem")
    value = await wait_redis_value(
        redis_address,
        key,
        timeout=20.0,
        match=(lambda item: str(item["timestamp"]) > after) if after else None,
    )
    return str(value["timestamp"])


async def _assert_link_healthy(proc: CollectorProcess, redis_address: str, duration: float) -> None:
    """损伤窗口中设备必须保持在线，Redis 最新值必须持续推进。"""
    before = await _wait_flow(redis_address)
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        status = await cli_status(proc)
        assert status is not None, "本地控制面 status 查询失败"
        assert status["devices_connected"] == 1, "链路损伤下出现假断连"
        await asyncio.sleep(0.5)
    await _wait_flow(redis_address, after=before)


class TestDelayAndJitter:
    async def test_delay_ladder_no_false_disconnect(
        self, netem_link: NetemLink, collector_factory, redis_service: str, tmp_path: Path
    ) -> None:
        """NET-01：10ms→50ms 延迟阶梯——无假断连、零重连、数据连续。"""
        proc, redis_address = await _start_stack(
            collector_factory, netem_link, tmp_path, redis_service
        )
        reconnects_base = await _reconnects(proc)

        for delay_ms in (10, 50):
            netem_link.apply(NetemScenario(name=f"delay_{delay_ms}ms", delay_ms=delay_ms))
            try:
                await _assert_link_healthy(proc, redis_address, duration=3.0)
            finally:
                netem_link.clear()

        assert await _reconnects(proc) == reconnects_base, "纯延迟不得触发任何重连（延迟不是断连）"
        await _assert_link_healthy(proc, redis_address, duration=1.0)

    async def test_jitter_no_reconnect_storm(
        self, netem_link: NetemLink, collector_factory, redis_service: str, tmp_path: Path
    ) -> None:
        """NET-02：20ms±10ms 抖动——健康状态稳定，无重连风暴。"""
        proc, redis_address = await _start_stack(
            collector_factory, netem_link, tmp_path, redis_service
        )
        reconnects_base = await _reconnects(proc)

        netem_link.apply(NetemScenario(name="jitter_20ms_10ms", delay_ms=20, jitter_ms=10))
        try:
            await _assert_link_healthy(proc, redis_address, duration=4.0)
        finally:
            netem_link.clear()

        assert await _reconnects(proc) == reconnects_base


class TestPacketLoss:
    async def test_low_loss_tolerated(
        self, netem_link: NetemLink, collector_factory, redis_service: str, tmp_path: Path
    ) -> None:
        """NET-03：1% 丢包零影响（TCP 重传吸收）；10% 丢包可容忍且恢复。

        1% 丢包下任何断连/重连都是健康度判定过敏——必须暴露。10% 丢包
        下允许极个别读超时（请求方向 TCP 重传预算内 4 次传输全丢概率
        ~1e-4/读），但绝不允许重连风暴，且清除后必须恢复。
        """
        proc, redis_address = await _start_stack(
            collector_factory, netem_link, tmp_path, redis_service
        )
        reconnects_base = await _reconnects(proc)

        netem_link.apply(NetemScenario(name="loss_1pct", loss_pct=1))
        try:
            await _assert_link_healthy(proc, redis_address, duration=5.0)
        finally:
            netem_link.clear()
        assert (
            await _reconnects(proc) == reconnects_base
        ), "1% 丢包不得触发重连——健康度判定对丢包过敏"

        netem_link.apply(NetemScenario(name="loss_10pct", loss_pct=10))
        try:
            await asyncio.sleep(5.0)
        finally:
            netem_link.clear()
        assert (await _reconnects(proc)) - reconnects_base <= 1, "10% 丢包下出现重连风暴"
        await wait_status(
            proc,
            lambda p: p["devices_connected"] == 1,
            timeout=15.0,
            description="device connected after 10% loss cleared",
        )
        await _assert_link_healthy(proc, redis_address, duration=1.0)

    async def test_blackhole_detected_and_recovers(
        self, netem_link: NetemLink, collector_factory, redis_service: str, tmp_path: Path
    ) -> None:
        """NET-04：黑洞（100% 丢包）→ 读超时判断连 → 退避重连 → 清除后恢复。

        与「停 modbus 服务」不同：黑洞下 TCP 连接本身不死，只能靠读
        超时检测——这是现场基站掉线的真实形态。
        """
        proc, redis_address = await _start_stack(
            collector_factory, netem_link, tmp_path, redis_service
        )
        reconnects_base = await _reconnects(proc)

        netem_link.apply(NetemScenario(name="blackhole", loss_pct=100))
        try:
            await wait_status(
                proc,
                lambda p: p["devices_connected"] == 0,
                timeout=20.0,
                description="blackhole detected as disconnect",
            )

            # 黑洞持续 12s：connect 尝试必须被退避节流。一个完整失败周期
            # 的预算为 health_timeout(0.5) + close 上界(1.0) +
            # connect_timeout(1.0) + interval(0.5) ≈ 3s，12s 窗口跨
            # 3~4 个周期且与快照相位无关地保证至少观察到 1 次；若无节流
            # （每个采集 tick 都尝试），同期会有 ~60 次，上界 10 严格区分。
            #
            # 已知生产缺陷 FINDING-RELIAB-BH-01（本用例复现，修复前稳定
            # 失败于 attempts == 0）：watchdog 以 asyncio.timeout 取消挂起
            # 的 connect，session.connect() 内抛 CancelledError（BaseException
            # 子类，非 Exception），DeviceRuntime._make_watchdog 的记账包装
            # `except Exception` 捕获不到，黑洞期每次 connect 超时都漏记
            # device_connect_failures——尝试真实发生（collector 日志可见
            # "watchdog cycle failed"），指标却恒为 0。
            first = await metrics_snapshot(proc.grpc_target)
            await asyncio.sleep(12.0)
            last = await metrics_snapshot(proc.grpc_target)
            attempts = last["device_connect_failures"].get(DEVICE_ID, 0) - first[
                "device_connect_failures"
            ].get(DEVICE_ID, 0)
            runs = last["counters"]["acquisition_runs"] - first["counters"]["acquisition_runs"]
            assert 1 <= attempts <= 10, f"黑洞期 connect 尝试未被退避节流或失败未记账: {attempts}"
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
        await _assert_link_healthy(proc, redis_address, duration=1.0)


class TestShortFlap:
    async def test_sub_timeout_flap_does_not_reconnect(
        self, netem_link: NetemLink, collector_factory, redis_service: str, tmp_path: Path
    ) -> None:
        """NET-05a：500ms 闪断（< 读超时 2s）——TCP 重传吸收，不得重建连接。"""
        proc, redis_address = await _start_stack(
            collector_factory, netem_link, tmp_path, redis_service
        )
        reconnects_base = await _reconnects(proc)

        await netem_link.outage(0.5)

        await _assert_link_healthy(proc, redis_address, duration=1.5)
        assert (
            await _reconnects(proc) == reconnects_base
        ), "500ms 闪断不得触发连接重建（亚超时中断应由 TCP 重传吸收）"

    async def test_timeout_boundary_flap_recovers(
        self, netem_link: NetemLink, collector_factory, redis_service: str, tmp_path: Path
    ) -> None:
        """NET-05b：2s 闪断（≈读超时）——允许一次断连重连，但必须自动恢复。"""
        proc, redis_address = await _start_stack(
            collector_factory, netem_link, tmp_path, redis_service
        )

        await netem_link.outage(2.0)

        await wait_status(
            proc,
            lambda p: p["devices_connected"] == 1,
            timeout=15.0,
            description="link healthy after 2s flap",
        )
        await _assert_link_healthy(proc, redis_address, duration=1.0)

    async def test_supra_timeout_flap_reconnects(
        self, netem_link: NetemLink, collector_factory, redis_service: str, tmp_path: Path
    ) -> None:
        """NET-05c：5s 闪断（> 读超时）——必须判断连、退避、恢复后重连出数。"""
        proc, redis_address = await _start_stack(
            collector_factory, netem_link, tmp_path, redis_service
        )
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
        await _assert_link_healthy(proc, redis_address, duration=1.0)
