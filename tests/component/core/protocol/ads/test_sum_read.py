"""Sum Read：请求顺序、逐点质量、分块与短应答校验。"""

from __future__ import annotations

import struct

import pytest

from core.application.errors import ProtocolError
from core.application.protocol_contract import Quality
from tests.support.ads_fake import FakePyadsConnection, make_driver, make_point, patch_session


def _driver_with(points: dict, *, max_subs_per_sum: int = 500):
    driver = make_driver(points, options={"max_subs_per_sum": max_subs_per_sum})
    connection = patch_session(driver, FakePyadsConnection())
    return driver, connection


async def test_empty_read_returns_empty_without_network() -> None:
    driver, _ = _driver_with({"p": make_point("p", index_group=0x4020, index_offset=0)})
    assert await driver.read_many(()) == ()


async def test_sum_read_preserves_order_and_decodes_values() -> None:
    points = {
        "a": make_point("a", data_type="INT", index_group=0x4020, index_offset=0),
        "b": make_point("b", data_type="INT", index_group=0x4020, index_offset=2),
        "c": make_point("c", data_type="INT", index_group=0x4020, index_offset=4),
    }
    driver, connection = _driver_with(points)
    connection.sum_read_store = {
        (0x4020, 0): struct.pack("<h", 11),
        (0x4020, 2): struct.pack("<h", -22),
        (0x4020, 4): struct.pack("<h", 33),
    }
    await driver.connect()
    samples = await driver.read_many(("c", "a", "b"))
    assert [sample.point_id for sample in samples] == ["c", "a", "b"]
    assert [sample.value for sample in samples] == [33, 11, -22]
    assert all(sample.quality is Quality.GOOD for sample in samples)
    # wire 请求按调用顺序携带三元组。
    items = [args for name, args in connection.calls if name == "read_write_sumread"][0]
    assert items == ((0x4020, 4, 2), (0x4020, 0, 2), (0x4020, 2, 2))
    await driver.close()


async def test_sum_read_per_point_device_error_is_bad_quality_not_failure() -> None:
    points = {
        "a": make_point("a", data_type="DINT", index_group=0x4020, index_offset=0),
        "b": make_point("b", data_type="DINT", index_group=0x4020, index_offset=4),
    }
    driver, connection = _driver_with(points)

    def override(items):
        errors = struct.pack("<II", 0, 1794)
        data = b"\x01\x00\x00\x00" + b"\x00" * 4
        return errors + data

    connection.sum_read_override = override
    await driver.connect()
    samples = await driver.read_many(("a", "b"))
    assert samples[0].quality is Quality.GOOD and samples[0].value == 1
    assert samples[1].quality is Quality.BAD and samples[1].value is None
    await driver.close()


async def test_sum_read_decode_failure_marks_bad_quality() -> None:
    # 非法 UTF-8 字节使 STRING 解码失败：逐点降级为 BAD，不拖垮整批。
    points = {"a": make_point("a", data_type="STRING", size=4, index_group=0x4020, index_offset=0)}
    driver, connection = _driver_with(points)
    connection.sum_read_store[(0x4020, 0)] = b"\xff\xff\xff\xff"
    await driver.connect()
    samples = await driver.read_many(("a",))
    assert samples[0].quality is Quality.BAD
    await driver.close()


async def test_sum_read_short_reply_is_protocol_error() -> None:
    points = {"a": make_point("a", data_type="INT", index_group=0x4020, index_offset=0)}
    driver, connection = _driver_with(points)
    connection.sum_read_override = lambda items: b"\x00" * 5  # 缺 1 字节
    await driver.connect()
    with pytest.raises(ProtocolError, match="shorter than expected"):
        await driver.read_many(("a",))
    await driver.close()


async def test_sum_read_chunks_at_limit_and_preserves_global_order() -> None:
    points = {
        f"p{i}": make_point(f"p{i}", data_type="INT", index_group=0x4020, index_offset=2 * i)
        for i in range(5)
    }
    driver, connection = _driver_with(points, max_subs_per_sum=2)
    connection.sum_read_override = lambda items: b"\x00" * (6 * len(items))
    await driver.connect()
    samples = await driver.read_many(tuple(f"p{i}" for i in range(5)))
    assert [sample.point_id for sample in samples] == [f"p{i}" for i in range(5)]
    requests = [args for name, args in connection.calls if name == "read_write_sumread"]
    assert [len(request) for request in requests] == [2, 2, 1]
    await driver.close()


async def test_sum_read_communication_failure_propagates() -> None:
    driver, connection = _driver_with({"a": make_point("a", index_group=0x4020, index_offset=0)})
    connection.fail["read_write"] = OSError("ams tcp reset")
    await driver.connect()
    with pytest.raises(OSError, match="ams tcp reset"):
        await driver.read_many(("a",))
    await driver.close()
