"""Sum Write：逐点确认、UNKNOWN 语义、分块未发送不重发与本地校验失败。"""

from __future__ import annotations

from core.application.protocol_contract import ProtocolWrite, WriteOutcome
from core.infrastructure.protocol.ads.session import ADSCommandNotSentError
from tests.support.ads_fake import FakePyadsConnection, make_driver, make_point, patch_session


def _driver_with(points: dict, *, max_subs_per_sum: int = 500):
    driver = make_driver(points, options={"max_subs_per_sum": max_subs_per_sum})
    connection = patch_session(driver, FakePyadsConnection())
    return driver, connection


def _write(point: str, value: object) -> ProtocolWrite:
    return ProtocolWrite(point=point, value=value)


async def test_empty_write_is_noop() -> None:
    driver, connection = _driver_with({"p": make_point("p", index_group=0x4020, index_offset=0)})
    assert await driver.write_many(()) == ()
    assert connection.calls == []


async def test_success_and_device_per_point_failure() -> None:
    points = {
        "a": make_point("a", data_type="INT", index_group=0x4020, index_offset=0),
        "b": make_point("b", data_type="INT", index_group=0x4020, index_offset=2),
    }
    driver, connection = _driver_with(points)
    connection.sum_write_errors = [0, 1794]
    await driver.connect()
    results = await driver.write_many((_write("a", 7), _write("b", 8)))
    assert results[0].outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert results[0].message is None
    assert results[1].outcome is WriteOutcome.CONFIRMED_FAILURE
    assert "1794" in (results[1].message or "")
    assert connection.written[(0x4020, 0)] == (7).to_bytes(2, "little", signed=True)
    assert connection.written[(0x4020, 2)] == (8).to_bytes(2, "little", signed=True)
    await driver.close()


async def test_local_coercion_failure_is_confirmed_without_network() -> None:
    driver, connection = _driver_with(
        {"a": make_point("a", data_type="INT", index_group=0x4020, index_offset=0)}
    )
    await driver.connect()
    results = await driver.write_many((_write("a", 3.5),))
    assert results[0].outcome is WriteOutcome.CONFIRMED_FAILURE
    assert connection.written == {}
    assert all(name != "read_write" for name, _ in connection.calls)
    await driver.close()


async def test_communication_failure_after_dispatch_is_unknown_without_resend() -> None:
    points = {
        "a": make_point("a", data_type="INT", index_group=0x4020, index_offset=0),
        "b": make_point("b", data_type="INT", index_group=0x4020, index_offset=2),
    }
    driver, connection = _driver_with(points, max_subs_per_sum=1)
    connection.fail["read_write"] = OSError("ams reset")
    await driver.connect()
    results = await driver.write_many((_write("a", 1), _write("b", 2)))
    # 第一块已下发：设备状态不可知 → UNKNOWN；后续块确定未发送 → CONFIRMED_FAILURE。
    by_point = {result.point_id: result for result in results}
    assert by_point["a"].outcome is WriteOutcome.UNKNOWN
    assert by_point["b"].outcome is WriteOutcome.CONFIRMED_FAILURE
    assert "not sent" in (by_point["b"].message or "")
    # 绝不在通信失败路径自动重发：只有一次物理 Sum Write。
    wire = [name for name, _ in connection.calls if name == "read_write"]
    assert len(wire) == 1
    await driver.close()


async def test_not_sent_rejection_is_confirmed_failure() -> None:
    driver, connection = _driver_with(
        {"a": make_point("a", data_type="INT", index_group=0x4020, index_offset=0)}
    )
    connection.fail["read_write"] = ADSCommandNotSentError("rejected before dispatch")
    await driver.connect()
    results = await driver.write_many((_write("a", 1),))
    assert results[0].outcome is WriteOutcome.CONFIRMED_FAILURE
    await driver.close()


async def test_write_chunks_and_bool_strict_typing() -> None:
    points = {
        f"p{i}": make_point(
            f"p{i}",
            data_type="BOOL",
            index_group=0x4020,
            index_offset=i,
        )
        for i in range(3)
    }
    driver, connection = _driver_with(points, max_subs_per_sum=2)
    await driver.connect()
    results = await driver.write_many((_write("p0", True), _write("p1", False), _write("p2", True)))
    assert [result.outcome for result in results] == [WriteOutcome.CONFIRMED_SUCCESS] * 3
    requests = [name for name, _ in connection.calls if name == "read_write_sumwrite"]
    assert len(requests) == 2
    await driver.close()
