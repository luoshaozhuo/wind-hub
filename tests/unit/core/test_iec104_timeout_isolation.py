"""请求超时后的会话隔离：旧响应不得被新请求接收。"""

import asyncio

import pytest

from core.application.errors import ProtocolConnectionError
from core.infrastructure.protocol.iec104.driver import IEC104Driver, _ReadRequest, _Session


@pytest.mark.asyncio
async def test_timeout_invalidates_session_and_all_pending_futures() -> None:
    driver = object.__new__(IEC104Driver)
    loop = asyncio.get_running_loop()
    receiving = loop.create_future()

    async def receive_worker() -> None:
        await receiving

    task = asyncio.create_task(receive_worker())
    session = _Session(conn=object(), task=task)
    read = loop.create_future()
    command = loop.create_future()
    interrogation = loop.create_future()
    session.reads[(1, 100)] = _ReadRequest(future=read, point_id="p")
    session.commands[(1, 200)] = command
    session.interrogations[1] = interrogation
    session.interrogation_qoi[1] = 21
    session.interrogation_samples[1] = []
    driver._session = session

    driver._invalidate_session(session, "timed out")
    assert driver._session is None
    assert session.failed
    assert not session.enabled
    assert task.cancelling()
    assert not session.reads
    assert not session.commands
    assert not session.interrogations
    assert not session.interrogation_qoi
    assert not session.interrogation_samples
    for future in (read, command, interrogation):
        with pytest.raises(ProtocolConnectionError, match="timed out"):
            await future

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_old_timeout_cannot_invalidate_replacement_session() -> None:
    driver = object.__new__(IEC104Driver)
    old = _Session(conn=object(), task=None)
    current = _Session(conn=object(), task=None)
    driver._session = current
    driver._invalidate_session(old, "old timeout")
    assert driver._session is current
    assert not current.failed
