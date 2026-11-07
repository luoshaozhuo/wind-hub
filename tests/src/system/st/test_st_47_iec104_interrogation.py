"""ST-47 IEC104 召唤部分完成/超时/自发上送交错（设计 §14.3 ST-47）。

对端为 c104/hat 真实回环栈；通过生产 _dispatch 路径注入受控 ASDU，
覆盖真实驱动的召唤状态机与监听路由，不替换被测驱动：
1. 激活后仅收到部分点值即超时——抛 IEC104InterrogationIncompleteError，
   partial samples 可观察，completed=False，绝不伪称完整。
2. 召唤未决期间夹杂自发上送（COT=3）——样本只路由到 Listener，
   不被吞进召唤结果。
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
from tests.support.iec104_fake import floating_data_msg

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.iec104,
    pytest.mark.timeout,
]


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


async def test_st_47_spontaneous_traffic_during_interrogation_routes_to_listeners(
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
        # 自发上送（999.0）必须送达 Listener；召唤数据（1111.0）按生产合同
        # 同时镜像给 Listener（driver._dispatch 对 COT 20..41 双路由）。
        listener_values = [sample.value for sample in spontaneous]
        assert 999.0 in [pytest.approx(v) for v in listener_values]
        assert 1111.0 in [pytest.approx(v) for v in listener_values]
        # 但召唤 partial 结果只收 COT 匹配的召唤数据，自发样本不得混入。
        assert [sample.point_id for sample in error.samples] == ["power"]
        assert error.samples[0].value == pytest.approx(1111.0)
        assert all(sample.value != pytest.approx(999.0) for sample in error.samples)
    finally:
        await listener.close()
        await driver.close()
