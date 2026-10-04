"""Component：监听型 Sink 的资源稳定性与客户端抖动隔离。

RES-03：10 次 listener rebuild（热重载 SCADA 链路）后，asyncio task 数
与进程 FD 数必须收敛——每次 rebuild 泄漏一个 server socket/handler
task 的话，长期运行的现场网关会在数十次 reload 后耗尽 FD。

SINK-05：SCADA 客户端反复断开/重连（含 RST 异常掉线）不得打挂
listener——listener 保持可服务、已服务的值正确，且同进程的其它
sink 经 Runtime 派发路径收数不受影响。

配置/helper 与 test_listener_sink_reload.py 同型（ModbusSink 监听
127.0.0.1:<port>，wt01.power → holding 100，float32）。
"""

from __future__ import annotations

import asyncio
import os
import struct

import pytest
from pymodbus.client import AsyncModbusTcpClient

from tests.support.process import free_port
from wind_hub_collector.adapter.outbound.sink.modbus import ModbusSink
from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.application.runtime import Runtime
from wind_hub_collector.domain.acquisition import AcquisitionEngine
from wind_hub_core.config.schema import RuntimeConfig
from wind_hub_core.config.sinks import ResolvedSinkConfig
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointValue

pytestmark = pytest.mark.real_service

REBUILD_CYCLES = 10
#: listener socket 关闭经 TIME_WAIT 等内核延迟释放，允许极小 FD 抖动；
#: 但每次 rebuild 泄漏 >=1 FD 的回归（10 次 >= +10）必然越界。
FD_SLACK = 2


def _modbus_config(port: int) -> ResolvedSinkConfig:
    return ResolvedSinkConfig(
        name="modbus_scada",
        type="modbus",
        connection={"host": "127.0.0.1", "port": port},
        points=[
            {
                "source": {"device_id": "wt01", "point_id": "power"},
                "ref": "wt01.power",
                "source_data_type": "float32",
                "source_unit": "none",
                "datatype": "float32",
                "unit": "none",
                "address": {
                    "unit_id": 1,
                    "register_type": "holding",
                    "address": 100,
                },
            }
        ],
    )


def _runtime(sinks: dict[str, SinkPort]) -> Runtime:
    return Runtime(
        devices={},
        sinks=sinks,
        engine=AcquisitionEngine(read_timeout=None),
        config=RuntimeConfig(shutdown_timeout=1.0, connect_timeout=0.5, read_timeout=0.5),
        tasks={},
    )


def _float32_registers(value: float) -> list[int]:
    return list(struct.unpack(">HH", struct.pack(">f", value)))


def _point(value: float) -> PointValue:
    return PointValue(device_id="wt01", point_id="power", value=value)


async def _read_listener_value(port: int) -> float:
    """以真实 Modbus 主站回读 listener 当前值（独立连接，与被测侧无共享状态）。"""
    client = AsyncModbusTcpClient("127.0.0.1", port=port)
    assert await client.connect()
    try:
        response = await client.read_holding_registers(100, count=2, device_id=1)
        assert not response.isError()
        return struct.unpack(">f", struct.pack(">HH", *response.registers))[0]
    finally:
        client.close()


async def _wait_listener_value(
    port: int, expected: float, timeout: float = 5.0
) -> None:
    """轮询回读直到 listener 服务到期望值。

    dispatch 只保证入队；consumer 落账到 listener 快照存储是异步的，
    直接读会竞态——这里是确定性轮询（每次读都是真实协议往返），不是
    sleep 掩盖。
    """
    deadline = asyncio.get_running_loop().time() + timeout
    last: float | None = None
    while asyncio.get_running_loop().time() < deadline:
        last = await _read_listener_value(port)
        if last == pytest.approx(expected):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"listener 未服务到 {expected}（最后读到 {last}）")


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


class _RecordingSink(SinkPort):
    """记录全部收到批次的 Sink（SINK-05 的「其它 sink」隔离判据）。"""

    def __init__(self) -> None:
        self.received: list[PointValue] = []

    async def open(self) -> None:
        """无外部资源。"""

    async def close(self) -> None:
        """无外部资源。"""

    async def write(self, batch: list[PointValue]) -> None:
        self.received.extend(batch)

    async def flush(self) -> None:
        """无缓冲。"""

    def health(self) -> HealthStatus:
        return HealthStatus(healthy=True)


class TestListenerRebuildResourceStability:
    async def test_rebuild_x10_no_task_or_fd_leak(self) -> None:
        """RES-03：10 次 rebuild 后 task/FD 收敛，且 listener 持续服务最新值。"""
        port = free_port()
        cfg = _modbus_config(port)
        first = ModbusSink(cfg)
        await first.write([_point(0.0)])
        rt = _runtime({"modbus_scada": first})
        await rt.start()
        try:
            await rt.rebuild_sink("modbus_scada", cfg, await _next_sink(cfg, 1.0))
            assert await _read_listener_value(port) == pytest.approx(1.0)

            tasks_baseline = len(asyncio.all_tasks())
            fds_baseline = _fd_count()

            for cycle in range(2, REBUILD_CYCLES + 1):
                await rt.rebuild_sink(
                    "modbus_scada", cfg, await _next_sink(cfg, float(cycle))
                )
                assert await _read_listener_value(port) == pytest.approx(
                    float(cycle)
                ), f"rebuild 第 {cycle} 次后 listener 未服务最新值"

            tasks_after = len(asyncio.all_tasks())
            fds_after = _fd_count()
            assert tasks_after <= tasks_baseline, (
                f"rebuild 泄漏 asyncio task: {tasks_baseline} → {tasks_after}"
            )
            assert fds_after <= fds_baseline + FD_SLACK, (
                f"rebuild 泄漏 FD: {fds_baseline} → {fds_after}"
            )
            assert rt.sinks["modbus_scada"].health().healthy is True
        finally:
            await rt.stop()


async def _next_sink(cfg: ResolvedSinkConfig, value: float) -> ModbusSink:
    """构造预置最新快照的新 sink 实例（与热重载的「先写快照再切换」一致）。"""
    sink = ModbusSink(cfg)
    # sink 未 open 前 write 仅更新快照存储；与 test_listener_sink_reload 同约定。
    await sink.write([_point(value)])
    return sink


class TestListenerClientFlapIsolation:
    async def test_scada_client_flap_does_not_break_listener_or_other_sinks(
        self,
    ) -> None:
        """SINK-05：客户端反复断开/RST 后 listener 健康、值正确、他 sink 无影响。"""
        port = free_port()
        listener = ModbusSink(_modbus_config(port))
        recording = _RecordingSink()
        rt = _runtime({"modbus_scada": listener, "rec": recording})
        await rt.start()
        try:
            # 初始值经 Runtime 派发路径送达两个 sink。
            await rt.dispatch({"modbus_scada": [_point(1.0)], "rec": [_point(1.0)]})
            await _wait_listener_value(port, 1.0)

            tasks_baseline = len(asyncio.all_tasks())
            fds_baseline = _fd_count()

            # ---- 客户端抖动：10 次正常断连 + 5 次 RST 异常掉线 ----
            for cycle in range(10):
                value = float(cycle + 2)
                await rt.dispatch(
                    {"modbus_scada": [_point(value)], "rec": [_point(value)]}
                )
                await _wait_listener_value(port, value)
                if cycle % 2 == 0:
                    await _rst_abort(port)

            # ---- 故障后：listener 健康、服务最新值、其它 sink 收齐全部批次 ----
            assert listener.health().healthy is True
            await _wait_listener_value(port, 11.0)
            expected = 11  # 初始 1 批 + 抖动期 10 批，每批 1 点
            assert len(recording.received) == expected
            assert recording.health().healthy is True

            assert len(asyncio.all_tasks()) <= tasks_baseline + 1, (
                "客户端抖动泄漏 handler task"
            )
            assert _fd_count() <= fds_baseline + FD_SLACK, (
                "客户端抖动泄漏 socket FD"
            )
        finally:
            await rt.stop()


async def _rst_abort(port: int) -> None:
    """建立 TCP 连接后立刻 RST（abort），模拟 SCADA 客户端异常掉线。"""
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.transport.abort()
    await asyncio.sleep(0)  # 让对端处理 reset 事件
