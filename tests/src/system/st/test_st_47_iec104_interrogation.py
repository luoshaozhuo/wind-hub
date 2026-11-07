"""ST-47 IEC104 召唤部分完成/超时/自发上送交错与 COT 路由隔离（设计 §14.3 ST-47）。

对端为 c104/hat 真实回环栈；通过生产 _dispatch 路径注入受控 ASDU，
覆盖真实驱动的召唤状态机与监听路由，不替换被测驱动：

1. 激活后仅收到部分点值即超时——抛 IEC104InterrogationIncompleteError，
   partial samples 可观察，completed=False，绝不伪称完整。
2. 召唤未决期间夹杂自发上送（COT=3）——自发样本只路由到 Listener；
   召唤数据（COT 20..41）按设计 §7.4.4/§7.4.5 只属于匹配的召唤上下文，
   不得镜像到 Listener（listen/interrogate 为两个独立入口）。
3. 无活动召唤时到达的召唤 COT——静默丢弃，不投递 Listener、不抛异常。
4. 错误 CA/IOA/Type/COT 的召唤数据——不进入召唤样本、不投递 Listener，
   且不影响后续正确帧的归集。
5. COT 1/2/3（周期/背景扫描/自发）均属监听流量，全部投递 Listener。
6. 并发 Listener：自发流量扇出到每个 Listener，召唤流量对所有 Listener 隔离。
"""

from __future__ import annotations

import asyncio

import pytest
from hat.drivers import iec101

from core.application.protocol_contract import InterrogationRequest
from core.domain import ConnectionEndpoint, PointAccess
from core.infrastructure.protocol.iec104 import IEC104Driver
from core.infrastructure.protocol.iec104.driver import (
    IEC104InterrogationIncompleteError,
)
from tests.core.protocol.conftest import make_point, make_table
from tests.support.iec104_fake import floating_data_msg, quality

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.iec104,
    pytest.mark.timeout,
]


def _single_data_msg(ca: int, ioa: int, *, cause: object) -> iec101.DataMsg:
    """与 M_ME_NC_1 点类型不匹配的 M_SP_NA_1 单点监视帧。"""
    return iec101.DataMsg(
        is_test=False,
        originator_address=1,
        asdu_address=ca,
        io_address=ioa,
        data=iec101.SingleData(iec101.SingleValue.ON, quality()),
        time=None,
        cause=cause,
    )


async def _driver_with_short_interrogation_timeout(port: int) -> IEC104Driver:
    table = make_table(
        "iec104",
        make_point("power", access=PointAccess.READ, ca=1, ioa=100, type_id="M_ME_NC_1"),
    )
    driver = IEC104Driver(
        ConnectionEndpoint("127.0.0.1", port),
        table,
        {"interrogation_timeout": 0.5},
    )
    await driver.connect()
    return driver


async def test_st_47_partial_interrogation_times_out_without_false_completion(
    iec104_server,
) -> None:
    driver = await _driver_with_short_interrogation_timeout(iec104_server.port)
    session = driver._session

    async def send_partial_then_silence(messages: object) -> None:
        # 激活帧不外发真实对端：召唤永不会收到结束确认，制造确定性超时。
        pending = driver._interrogation[1]
        pending.accepted = True
        # 仅 power 一个点的部分数据（真实生产分发路径）。
        driver._dispatch(
            session,
            [
                floating_data_msg(
                    1,
                    100,
                    1234.5,
                    cause=iec101.DataResCause.INTERROGATED_STATION,
                )
            ],
        )

    driver._session.send = send_partial_then_silence  # type: ignore[method-assign]
    try:
        with pytest.raises(IEC104InterrogationIncompleteError) as exc_info:
            await driver.interrogate(InterrogationRequest(kind="station", common_address=1))
        error = exc_info.value
        assert error.completed is False
        assert error.common_address == 1
        assert [sample.point_id for sample in error.samples] == ["power"]
        assert error.samples[0].value == pytest.approx(1234.5)
        assert "incomplete" in str(error)
        # 超时后召唤状态被清理，允许下一轮召唤重新建立。
        assert driver._interrogation == {}
    finally:
        await driver.close()


async def test_st_47_spontaneous_reaches_listener_but_interrogation_data_is_isolated(
    iec104_server,
) -> None:
    driver = await _driver_with_short_interrogation_timeout(iec104_server.port)
    session = driver._session
    spontaneous: list = []
    got_spontaneous = asyncio.Event()

    async def on_spontaneous(batch: tuple) -> None:
        spontaneous.extend(batch)
        got_spontaneous.set()

    listener = await driver.listen(["power"], on_spontaneous)

    async def send_with_spontaneous_interleave(_messages: object) -> None:
        pending = driver._interrogation[1]
        pending.accepted = True
        # 召唤未决期间到达一条自发上送（COT=3）与一条召唤数据（COT=20）。
        driver._dispatch(
            session,
            [
                floating_data_msg(1, 100, 999.0, cause=iec101.DataResCause.SPONTANEOUS),
                floating_data_msg(1, 100, 1111.0, cause=iec101.DataResCause.INTERROGATED_STATION),
            ],
        )

    driver._session.send = send_with_spontaneous_interleave  # type: ignore[method-assign]
    try:
        # 召唤并发执行：其 send 内部分发交错 ASDU，自发样本经 Listener 异步投递。
        interrogate_task = asyncio.create_task(
            driver.interrogate(InterrogationRequest(kind="station", common_address=1))
        )
        await asyncio.wait_for(got_spontaneous.wait(), timeout=3.0)
        with pytest.raises(IEC104InterrogationIncompleteError) as exc_info:
            await interrogate_task
        error = exc_info.value
        # 设计 §7.4.4：_dispatch 按 COT 分流；§7.4.5：listen 与 interrogate
        # 是独立入口。Listener 只能收到自发上送（999.0），召唤数据（1111.0）
        # 属召唤上下文专有，禁止双路由镜像。
        await asyncio.sleep(0.05)  # 让任何错误投递有机会浮入监听队列
        listener_values = [sample.value for sample in spontaneous]
        assert listener_values == [pytest.approx(999.0)]
        assert all(sample.value != pytest.approx(1111.0) for sample in spontaneous)
        # 召唤 partial 结果只收 COT 匹配的召唤数据，自发样本不得混入。
        assert [sample.point_id for sample in error.samples] == ["power"]
        assert error.samples[0].value == pytest.approx(1111.0)
        assert all(sample.value != pytest.approx(999.0) for sample in error.samples)
    finally:
        await listener.close()
        await driver.close()


@pytest.mark.parametrize(
    "cause",
    [
        iec101.DataResCause.INTERROGATED_STATION,
        iec101.DataResCause.INTERROGATED_GROUP01,
        iec101.DataResCause.INTERROGATED_COUNTER,
    ],
)
async def test_st_47_interrogation_cot_without_active_interrogation_is_dropped(
    iec104_server,
    cause: object,
) -> None:
    driver = await _driver_with_short_interrogation_timeout(iec104_server.port)
    delivered: list = []

    async def on_batch(batch: tuple) -> None:
        delivered.extend(batch)

    listener = await driver.listen(["power"], on_batch)
    try:
        # 没有任何活动召唤上下文：召唤 COT 帧必须静默丢弃，
        # 不冒充自发上送投递给 Listener，也不抛异常。
        driver._dispatch(
            driver._session,
            [floating_data_msg(1, 100, 7.0, cause=cause)],
        )
        await asyncio.sleep(0.05)
        assert delivered == []
        assert driver._interrogation == {}
        assert driver._listener_fault is False
    finally:
        await listener.close()
        await driver.close()


async def test_st_47_wrong_ca_ioa_type_and_cot_never_misroute(iec104_server) -> None:
    driver = await _driver_with_short_interrogation_timeout(iec104_server.port)
    session = driver._session
    delivered: list = []

    async def on_batch(batch: tuple) -> None:
        delivered.extend(batch)

    listener = await driver.listen(["power"], on_batch)

    async def send_malformed_then_valid(_messages: object) -> None:
        pending = driver._interrogation[1]
        pending.accepted = True
        # 错误 CA：CA=2 不存在活动召唤，直接丢弃。
        driver._dispatch(
            session,
            [floating_data_msg(2, 100, 1.0, cause=iec101.DataResCause.INTERROGATED_STATION)],
        )
        # 错误 IOA：召唤点表内无 (1,9999)。
        driver._dispatch(
            session,
            [floating_data_msg(1, 9999, 2.0, cause=iec101.DataResCause.INTERROGATED_STATION)],
        )
        # 错误 Type：power 配置 M_ME_NC_1，到达 M_SP_NA_1 单点帧。
        driver._dispatch(
            session,
            [_single_data_msg(1, 100, cause=iec101.DataResCause.INTERROGATED_STATION)],
        )
        # 错误 COT：站召期望 COT=20，组 1 帧 COT=21 不匹配。
        driver._dispatch(
            session,
            [floating_data_msg(1, 100, 3.0, cause=iec101.DataResCause.INTERROGATED_GROUP01)],
        )
        assert pending.samples == {}
        # 随后一条完全匹配的正确帧必须仍能被归集，证明错误帧无附带杀伤。
        driver._dispatch(
            session,
            [floating_data_msg(1, 100, 42.0, cause=iec101.DataResCause.INTERROGATED_STATION)],
        )
        assert list(pending.samples) == [(1, 100)]

    driver._session.send = send_malformed_then_valid  # type: ignore[method-assign]
    try:
        with pytest.raises(IEC104InterrogationIncompleteError) as exc_info:
            await driver.interrogate(InterrogationRequest(kind="station", common_address=1))
        error = exc_info.value
        # 只有正确帧进入召唤样本，四条错误帧全部不落账。
        assert [sample.value for sample in error.samples] == [pytest.approx(42.0)]
        await asyncio.sleep(0.05)
        # 召唤 COT 的任何帧（含错误帧）都不得进入 Listener。
        assert delivered == []
        assert driver._listener_fault is False
    finally:
        await listener.close()
        await driver.close()


@pytest.mark.parametrize(
    "cause",
    [
        iec101.DataResCause.PERIODIC,
        iec101.DataResCause.BACKGROUND_SCAN,
        iec101.DataResCause.SPONTANEOUS,
    ],
)
async def test_st_47_spontaneous_family_cot_1_2_3_routes_to_listener(
    iec104_server,
    cause: object,
) -> None:
    driver = await _driver_with_short_interrogation_timeout(iec104_server.port)
    received: list = []
    got = asyncio.Event()

    async def on_batch(batch: tuple) -> None:
        received.extend(batch)
        got.set()

    listener = await driver.listen(["power"], on_batch)
    try:
        driver._dispatch(
            driver._session,
            [floating_data_msg(1, 100, 5.5, cause=cause)],
        )
        await asyncio.wait_for(got.wait(), timeout=3.0)
        assert [sample.point_id for sample in received] == ["power"]
        assert received[0].value == pytest.approx(5.5)
        # COT 1/2/3 从不进入召唤归集。
        assert driver._interrogation == {}
    finally:
        await listener.close()
        await driver.close()


async def test_st_47_concurrent_listeners_fan_out_spontaneous_and_isolate_interrogation(
    iec104_server,
) -> None:
    driver = await _driver_with_short_interrogation_timeout(iec104_server.port)
    session = driver._session
    a_received: list = []
    b_received: list = []
    got_a = asyncio.Event()

    async def on_a(batch: tuple) -> None:
        a_received.extend(batch)
        got_a.set()

    async def on_b(batch: tuple) -> None:
        b_received.extend(batch)

    listener_a = await driver.listen(["power"], on_a)
    listener_b = await driver.listen(["power"], on_b)

    async def send_mixed(_messages: object) -> None:
        pending = driver._interrogation[1]
        pending.accepted = True
        driver._dispatch(
            session,
            [
                floating_data_msg(1, 100, 8.8, cause=iec101.DataResCause.SPONTANEOUS),
                floating_data_msg(1, 100, 9.9, cause=iec101.DataResCause.INTERROGATED_STATION),
            ],
        )

    driver._session.send = send_mixed  # type: ignore[method-assign]
    try:
        interrogate_task = asyncio.create_task(
            driver.interrogate(InterrogationRequest(kind="station", common_address=1))
        )
        await asyncio.wait_for(got_a.wait(), timeout=3.0)
        with pytest.raises(IEC104InterrogationIncompleteError):
            await interrogate_task
        await asyncio.sleep(0.05)
        # 自发流量扇出到两个并发 Listener；召唤数据对两者均不可见。
        assert [s.value for s in a_received] == [pytest.approx(8.8)]
        assert [s.value for s in b_received] == [pytest.approx(8.8)]
        assert driver._listener_fault is False
    finally:
        await listener_a.close()
        await listener_b.close()
        await driver.close()
