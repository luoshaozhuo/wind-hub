"""ST-45 IEC104 Listener 容量配置、满队列故障隔离与关闭收敛。

对端为 tests.fixtures.servers.iec104_server（c104/hat 真实回环栈）。
队列与失效逻辑为生产 _Listener 实现，通过真实 driver.listen() 建句柄、
生产 offer() 路径注入受控样本，不 mock 被测类。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from core.application.protocol_contract import ProtocolSample
from core.domain import ConnectionEndpoint
from core.infrastructure.protocol.iec104 import IEC104Driver
from tests.core.protocol.conftest import iec104_driver, make_point, make_table

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.iec104,
    pytest.mark.overload,
]


def _sample(point_id: str = "power") -> ProtocolSample:
    return ProtocolSample(point_id=point_id, value=1.0, timestamp=datetime.now(UTC))


async def test_st_45_listener_capacity_is_dynamically_configurable(iec104_server) -> None:
    default_driver = iec104_driver(iec104_server.port)
    await default_driver.connect()
    try:
        default_listener = await default_driver.listen(["power"], lambda batch: None)
        try:
            assert default_listener.queue.maxsize == 256  # 设计默认容量
        finally:
            await default_listener.close()
    finally:
        await default_driver.close()

    table = make_table(
        "iec104",
        make_point("power", access="READ", ca=1, ioa=100, type_id="M_ME_NC_1"),
    )
    bounded = IEC104Driver(
        ConnectionEndpoint("127.0.0.1", iec104_server.port),
        table,
        {"listener_queue_capacity": 1},
    )
    await bounded.connect()
    try:
        listener = await bounded.listen(["power"], lambda batch: None)
        try:
            assert listener.queue.maxsize == 1
        finally:
            await listener.close()
    finally:
        await bounded.close()


async def test_st_45_full_queue_invalidates_listener_without_silent_loss(iec104_server) -> None:
    driver = IEC104Driver(
        ConnectionEndpoint("127.0.0.1", iec104_server.port),
        make_table(
            "iec104",
            make_point("power", access="READ", ca=1, ioa=100, type_id="M_ME_NC_1"),
        ),
        {"listener_queue_capacity": 1},
    )
    await driver.connect()
    try:
        gate = asyncio.Event()

        async def blocked_callback(_batch: object) -> None:
            await gate.wait()  # 占住消费端，强制队列堆积至溢出

        victim = await driver.listen(["power"], blocked_callback)
        healthy_received: list = []

        async def healthy_callback(batch: tuple) -> None:
            healthy_received.extend(batch)

        healthy = await driver.listen(["power"], healthy_callback)
        try:
            victim.offer((_sample(),))  # 唯一队列位被占住
            victim.offer((_sample(),))  # 满队列：必须显式失效而非静默丢弃
            await asyncio.sleep(0.05)
            assert victim.failed is True
            assert driver._listener_fault is True
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(victim.task, timeout=2.0)

            # 故障隔离：另一个 Listener 继续投递，不被失效句柄拖累。
            healthy.offer((_sample(),))
            await asyncio.sleep(0.05)
            assert healthy.failed is False
            assert [item.point_id for item in healthy_received] == ["power"]
            gate.set()
        finally:
            await victim.close()
            await healthy.close()
    finally:
        await driver.close()


async def test_st_45_close_removes_listener_and_converges_bounded(iec104_server) -> None:
    driver = iec104_driver(iec104_server.port)
    await driver.connect()
    try:
        listener = await driver.listen(["power"], lambda batch: None)
        assert listener in driver._listeners
        await listener.close()
        # 句柄注销可确认收敛：路由集合移除，后台任务结束。
        assert listener not in driver._listeners
        assert listener.task.done()
        # 重复关闭不抛异常（幂等收敛）。
        await listener.close()
    finally:
        await driver.close()
