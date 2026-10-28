"""TDengineSink HTTP/REST 层与剩余生命周期分支测试。

build_opener 全 fake（不发起真实网络）：验证 Basic 认证头、URL、POST、
错误分类（HTTP/URL/超时/坏 JSON/SQL 拒绝）、open/write/health/diagnostic
状态机、非有限值剔除、超长 ID、任务白名单与 _blocking 取消语义。
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import urllib.request
from urllib.error import HTTPError, URLError

import pytest

from core.application import Quality
from core.application.sink_contract import ResolvedSink
from core.application.sink_error import SinkError
from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.sink.tdengine import TDengineSink


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


class FakeOpener:
    def __init__(self, responder) -> None:
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []
        self._responder = responder

    def open(self, request: urllib.request.Request, timeout: float):
        self.requests.append(request)
        self.timeouts.append(timeout)
        result = self._responder(request)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


def _install_opener(monkeypatch: pytest.MonkeyPatch, responder) -> FakeOpener:
    opener = FakeOpener(responder)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    return opener


def _sink(**connection) -> TDengineSink:
    values = {
        "host": "localhost",
        "port": 6041,
        "database": "wind_hub",
        "stable": "point_values",
        "username": "admin",
        "password": "s3cr3t",
        "timeout": 2.0,
    }
    values.update(connection)
    return TDengineSink(Sink(sink_id="history", kind="tdengine", enabled=True, connection=values))


_OK = json.dumps({"code": 0}).encode()


def test_execute_posts_basic_auth_and_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _install_opener(monkeypatch, lambda request: _OK)
    sink = _sink(tls=True)

    sink._execute("SELECT 1")

    request = opener.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://localhost:6041/rest/sql/wind_hub"
    assert opener.timeouts == [2.0]
    expected = "Basic " + base64.b64encode(b"admin:s3cr3t").decode()
    assert request.get_header("Authorization") == expected
    assert request.data == b"SELECT 1"


@pytest.mark.parametrize(
    ("error", "label"),
    [
        (HTTPError("url", 500, "server", hdrs=None, fp=None), "HTTPError"),
        (URLError("unreachable"), "URLError"),
        (TimeoutError(), "TimeoutError"),
    ],
)
def test_execute_transport_errors_are_classified(
    monkeypatch: pytest.MonkeyPatch, error: Exception, label: str
) -> None:
    _install_opener(monkeypatch, lambda request: error)
    sink = _sink()

    with pytest.raises(SinkError, match=f"TDengine request failed: {label}"):
        sink._execute("SELECT 1")


def test_execute_bad_json_is_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_opener(monkeypatch, lambda request: b"{not json")
    sink = _sink()
    with pytest.raises(SinkError, match="TDengine request failed: JSONDecodeError"):
        sink._execute("SELECT 1")


def test_execute_sql_rejected_with_description(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"code": 0x2602, "desc": "syntax error"}).encode()
    _install_opener(monkeypatch, lambda request: body)
    sink = _sink()
    with pytest.raises(SinkError, match="TDengine SQL rejected: syntax error"):
        sink._execute("BAD SQL")


def test_execute_non_mapping_body_is_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_opener(monkeypatch, lambda request: b"[1, 2]")
    sink = _sink()
    with pytest.raises(SinkError, match="invalid response"):
        sink._execute("SELECT 1")


async def test_open_failure_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_opener(monkeypatch, lambda request: URLError("down"))
    sink = _sink()
    with pytest.raises(SinkError, match="TDengine open failed"):
        await sink.open()
    assert (await sink.health()).healthy is False


async def test_write_requires_open() -> None:
    sink = _sink()
    with pytest.raises(SinkError, match="not opened"):
        await sink.write([PointValue("d", "p", 1)])


async def test_non_finite_only_batch_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _install_opener(monkeypatch, lambda request: _OK)
    sink = _sink()
    await sink.open()

    await sink.write(
        [
            PointValue("d", "p", float("nan"), quality=Quality.GOOD),
            PointValue("d", "q", float("inf"), quality=Quality.GOOD),
        ]
    )

    # 只有 open 的 DDL，没有任何 INSERT 请求。
    assert all(not request.data.startswith(b"INSERT") for request in opener.requests)
    await sink.close()
    with pytest.raises(SinkError, match="not opened"):
        await sink.write([PointValue("d", "p", 1)])


async def test_oversized_identifier_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_opener(monkeypatch, lambda request: _OK)
    sink = _sink()
    await sink.open()
    with pytest.raises(SinkError, match="192 characters"):
        await sink.write([PointValue("d", "p" * 193, 1)])


async def test_write_task_filters_by_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _install_opener(monkeypatch, lambda request: _OK)
    resolved = ResolvedSink(
        sink_id="history",
        kind="tdengine",
        enabled=True,
        connection={
            "host": "localhost",
            "port": 6041,
            "database": "wind_hub",
            "stable": "point_values",
            "username": "admin",
            "password": "s3cr3t",
        },
        task_sources={"task-a": (("wt01", "allowed"),)},
    )
    sink = TDengineSink(resolved)
    await sink.open()

    await sink.write_task(
        "task-a",
        [
            PointValue("wt01", "allowed", 1),
            PointValue("wt01", "denied", 2),
        ],
    )

    inserts = [request for request in opener.requests if request.data.startswith(b"INSERT")]
    assert len(inserts) == 1
    payload = inserts[0].data.decode()
    assert "denied" not in payload
    await sink.close()


async def test_health_and_diagnostic_states(monkeypatch: pytest.MonkeyPatch) -> None:
    def responder(request: urllib.request.Request):
        if request.data == b"SELECT SERVER_VERSION()":
            return URLError("timeout")
        return _OK

    _install_opener(monkeypatch, responder)
    sink = _sink()
    await sink.open()  # DDL 成功

    health = await sink.health()
    assert health.healthy is False
    # diagnostic 不掩盖底层失败原因。
    with pytest.raises(SinkError, match="URLError"):
        await sink.diagnostic()


async def test_blocking_cancellation_waits_for_inflight_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = _sink()
    finished = threading.Event()

    def slow_execute(_sql: str) -> None:
        finished.wait(0.5)
        finished.set()

    monkeypatch.setattr(sink, "_execute", slow_execute)

    blocked = asyncio.create_task(sink._blocking("SELECT 1"))
    await asyncio.sleep(0.05)
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    # shield 语义：取消时必须等在途请求落定，不留悬挂线程。
    assert finished.is_set()
