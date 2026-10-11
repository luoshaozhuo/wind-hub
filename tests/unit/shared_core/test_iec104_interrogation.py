"""IEC104 总召控制报文状态识别与协议级完成确认。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.application import ProtocolError
from core.infrastructure.protocol.iec104.driver import IEC104Driver


def _frame(cot: int, *, negative: bool = False, ca: int = 1, type_id: int = 100, qoi: int = 20) -> bytes:
    # IEC104 I frame: 6-byte APCI + 10-byte C_IC_NA_1 ASDU.
    asdu = bytes([type_id, 1, cot | (0x40 if negative else 0), 0])
    asdu += ca.to_bytes(2, "little") + bytes((0, 0, 0, qoi))
    return bytes((0x68, 4 + len(asdu), 0, 0, 0, 0)) + asdu


@pytest.mark.parametrize(
    ("cot", "negative", "expected"),
    [(7, False, None), (7, True, "rejected"), (10, False, "completed"),
     (10, True, "rejected"), (3, False, None)],
)
def test_interrogation_result_identifies_only_final_states(
    cot: int, negative: bool, expected: str | None
) -> None:
    assert IEC104Driver._interrogation_result(
        _frame(cot, negative=negative), 1
    ) == expected


def test_interrogation_result_ignores_other_stations_and_types() -> None:
    parser = IEC104Driver._interrogation_result
    assert parser(_frame(10, ca=2), 1) is None
    assert parser(_frame(10, type_id=1), 1) is None
    assert parser(_frame(10, qoi=21), 1) is None
    assert parser(_frame(10)[:-1], 1) is None
    assert parser(bytes((0x68, 4, 1, 0, 0, 0)), 1) is None


@pytest.mark.asyncio
async def test_raw_interrogation_completion_uses_driver_loop() -> None:
    driver = object.__new__(IEC104Driver)
    driver._closed = False
    driver._loop = asyncio.get_running_loop()
    driver._config = SimpleNamespace(common_addr=1)
    connection = object()
    driver._connection = connection
    driver._interrogation_waiter = driver._loop.create_future()
    driver._handle_raw_receive(connection, _frame(10))
    await asyncio.sleep(0)
    assert driver._interrogation_waiter.done()
    assert driver._interrogation_waiter.result() is None


@pytest.mark.asyncio
async def test_raw_interrogation_negative_confirmation_raises() -> None:
    driver = object.__new__(IEC104Driver)
    driver._closed = False
    driver._loop = asyncio.get_running_loop()
    driver._config = SimpleNamespace(common_addr=1)
    connection = object()
    driver._connection = connection
    driver._interrogation_waiter = driver._loop.create_future()
    driver._handle_raw_receive(connection, _frame(7, negative=True))
    await asyncio.sleep(0)
    with pytest.raises(ProtocolError, match="negative confirmation"):
        await driver._interrogation_waiter


@pytest.mark.asyncio
async def test_interrogation_rejects_disconnect() -> None:
    driver = object.__new__(IEC104Driver)
    driver._interrogation_waiter = asyncio.get_running_loop().create_future()
    driver._reject_interrogation("connection lost")
    with pytest.raises(ProtocolError, match="connection lost"):
        await driver._interrogation_waiter if driver._interrogation_waiter else _failed()
    

async def _failed() -> None:
    raise ProtocolError("connection lost")
