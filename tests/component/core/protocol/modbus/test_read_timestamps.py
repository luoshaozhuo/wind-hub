"""Modbus 读路径（GOOD/BAD/批量）样本均带必填 UTC 时间戳。"""

from __future__ import annotations

import pytest

from core.application import Quality

from .test_single_ops import _Client, _driver, _point, _Response


@pytest.mark.asyncio
async def test_modbus_read_one_good_and_bad_carry_local_timestamps() -> None:
    client = _Client()
    client.response = _Response(registers=[7])
    driver = _driver({"h": _point("h", "holding", 100)}, client)

    good = await driver.read_one("h")
    assert good.timestamp.tzinfo is not None
    assert good.timestamp_source == "local"

    client.response = _Response(registers=[])  # 解码失败 → BAD
    bad = await driver.read_one("h")
    assert bad.quality == Quality.BAD
    assert bad.timestamp.tzinfo is not None
    assert bad.timestamp_source == "local"


@pytest.mark.asyncio
async def test_modbus_read_many_shares_single_batch_timestamp() -> None:
    client = _Client()
    client.response = _Response(registers=[1, 2])
    driver = _driver(
        {"a": _point("a", "holding", 100), "b": _point("b", "holding", 101)},
        client,
    )

    samples = await driver.read_many(["a", "b"])
    assert all(s.timestamp.tzinfo is not None for s in samples)
    assert samples[0].timestamp == samples[1].timestamp
    assert all(s.timestamp_source == "local" for s in samples)
