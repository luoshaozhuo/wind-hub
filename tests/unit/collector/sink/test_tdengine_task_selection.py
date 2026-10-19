"""PR#92 Round4 回归：TDengine Task 选择性写入（D1）、非有限值逐点隔离（D4）、
host/重定向安全（D5）、诊断点 source 字面量（D6-1）。

无真实 taosAdapter：SQL 经 monkeypatch ``_execute`` 捕获；D5 使用本地 HTTP 服务器。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from pydantic import ValidationError

from core.application.sink_contract import ResolvedSink
from core.application.sink_error import SinkError
from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import TDengineSinkConnection
from core.infrastructure.sink.tdengine import TDengineSink

CONNECTION = {
    "host": "127.0.0.1",
    "port": 6041,
    "database": "wind_hub",
    "stable": "point_values",
    "username": "root",
    "password": "td-secret-value",
    "timeout": 5.0,
    "tls": False,
}


def make_sink(task_sources=None) -> tuple[TDengineSink, list[str]]:
    config: Sink
    if task_sources is None:
        config = Sink(sink_id="td", kind="tdengine", enabled=True, connection=CONNECTION)
    else:
        config = ResolvedSink(
            sink_id="td",
            kind="tdengine",
            enabled=True,
            connection=CONNECTION,
            points=(),
            task_sources=task_sources,
            task_key_prefixes=None,
        )
    sink = TDengineSink(config)
    statements: list[str] = []
    sink._execute = statements.append  # type: ignore[method-assign]
    sink._opened = True
    sink._error = None
    return sink, statements


def point_ids(statements: list[str]) -> set[str]:
    """从 INSERT 语句的 TAGS 段提取实际写入的 point_id。"""
    import re

    ids: set[str] = set()
    for sql in statements:
        if not sql.startswith("INSERT INTO "):
            continue
        ids.update(re.findall(r"TAGS \('[^']*', '([^']*)'\)", sql))
    return ids


TASK_SOURCES = {
    "taskA": (("dev1", "p1"), ("dev1", "p2")),
    "taskB": (("dev1", "p3"),),
}


# ---------- D1：Task 测点选择性写入 ----------


async def test_write_task_only_emits_selected_points() -> None:
    sink, statements = make_sink(TASK_SOURCES)
    await sink.write_task(
        "taskA",
        [
            PointValue("dev1", "p1", 1.0),
            PointValue("dev1", "p2", 2.0),
            PointValue("dev1", "p3", 3.0),  # taskA 未选择
        ],
    )
    assert point_ids(statements) == {"p1", "p2"}


async def test_two_tasks_share_sink_with_distinct_point_sets() -> None:
    sink, statements = make_sink(TASK_SOURCES)
    await sink.write_task(
        "taskB",
        [
            PointValue("dev1", "p1", 1.0),  # taskB 未选择
            PointValue("dev1", "p3", 3.0),
        ],
    )
    assert point_ids(statements) == {"p3"}


async def test_same_point_id_on_other_device_is_not_authorized() -> None:
    sink, statements = make_sink(TASK_SOURCES)
    await sink.write_task("taskA", [PointValue("dev2", "p1", 9.0)])
    assert statements == []


async def test_plain_write_delivers_full_batch() -> None:
    """无 Task 上下文的 write() 保持全量投递契约（write_test/直写路径）。"""
    sink, statements = make_sink(TASK_SOURCES)
    await sink.write([PointValue("dev1", "p1", 1.0), PointValue("devX", "pY", 2.0)])
    assert point_ids(statements) == {"p1", "pY"}


async def test_unknown_task_id_writes_nothing_without_error() -> None:
    sink, statements = make_sink(TASK_SOURCES)
    await sink.write_task("task-unknown", [PointValue("dev1", "p1", 1.0)])
    assert statements == []
    assert sink._error is None


async def test_non_resolved_sink_write_task_drops_task_scoped_batch() -> None:
    """裸 domain Sink 无 task_sources：write_task 无法判定授权，fail-closed 全丢弃；
    write() 仍全量。"""
    sink, statements = make_sink(None)
    await sink.write_task("taskA", [PointValue("dev1", "p1", 1.0)])
    assert statements == []
    await sink.write([PointValue("dev1", "p1", 1.0)])
    assert point_ids(statements) == {"p1"}


# ---------- D4：NaN/Inf 逐点隔离 ----------


@pytest.mark.parametrize(
    "values",
    [
        [float("nan")],
        [float("inf")],
        [float("-inf")],
        [float("nan"), float("inf"), float("-inf")],
    ],
)
async def test_all_nonfinite_batch_emits_nothing(values: list[float]) -> None:
    sink, statements = make_sink(TASK_SOURCES)
    await sink.write_task("taskA", [PointValue("dev1", "p1", v) for v in values])
    assert statements == []
    assert sink._error is None


async def test_nonfinite_at_head_middle_tail_keeps_valid_points() -> None:
    sink, statements = make_sink(TASK_SOURCES)
    good1 = PointValue("dev1", "p1", 1.0)
    good2 = PointValue("dev1", "p1", 2.0)
    good3 = PointValue("dev1", "p1", 3.0)
    await sink.write_task("taskA", [float_nan(good1.point_id), good1])
    await sink.write_task("taskA", [good2, float_inf(good2.point_id), good3])
    await sink.write_task("taskA", [good1, float_nan(good1.point_id)])
    # 每批的好点均落库：1 + 2 + 1 行
    emitted = sum(sql.count(" USING ") for sql in statements)
    assert emitted == 4


async def test_consecutive_batches_recover_after_nonfinite_batch() -> None:
    sink, statements = make_sink(TASK_SOURCES)
    await sink.write_task("taskA", [PointValue("dev1", "p1", float("nan"))])
    await sink.write_task("taskA", [PointValue("dev1", "p1", 7.5)])
    assert point_ids(statements) == {"p1"}
    assert "7.5" in statements[0]


@pytest.mark.parametrize("value", [0, 1, True, False, "ok", None])
async def test_non_float_scalar_values_pass_through(value: object) -> None:
    sink, statements = make_sink(TASK_SOURCES)
    await sink.write_task("taskA", [PointValue("dev1", "p1", value)])
    assert len(statements) == 1


def float_nan(point_id: str) -> PointValue:
    return PointValue("dev1", point_id, float("nan"))


def float_inf(point_id: str) -> PointValue:
    return PointValue("dev1", point_id, float("inf"))


# ---------- D6-1：诊断点 source 不再落字面量 None ----------


async def test_diagnostic_probe_source_falls_back_to_timestamp_source() -> None:
    sink, statements = make_sink(TASK_SOURCES)
    await sink.write([PointValue("_diagnostic", "_write_test", 1)])
    sql = statements[0]
    assert sql.rstrip().endswith("'local')")
    assert "'None'" not in sql


async def test_explicit_point_source_is_preserved() -> None:
    sink, statements = make_sink(TASK_SOURCES)
    await sink.write([PointValue("dev1", "p1", 1.0, source="iec104-driver")])
    assert "'iec104-driver'" in statements[0]


# ---------- D5：host 形态校验 ----------


@pytest.mark.parametrize(
    "host",
    [
        "evil.example/x?",
        "evil.example\\x",
        "root:pwd@evil.example",
        "evil.example?q=1",
        "evil#frag",
        "evil example",
        "evil\texample",
        "evil\nexample",
        "evil:8080",
        "::1",
        "2001:db8::1",
        "",
    ],
)
def test_malformed_host_rejected(host: str) -> None:
    with pytest.raises(ValidationError):
        TDengineSinkConnection.model_validate({**CONNECTION, "host": host})


@pytest.mark.parametrize(
    "host",
    ["taos.internal", "127.0.0.1", "10.0.0.1", "my-host_1.example.com", "TAOS01"],
)
def test_plain_hostname_and_ipv4_accepted(host: str) -> None:
    TDengineSinkConnection.model_validate({**CONNECTION, "host": host})


# ---------- D5：真实 HTTP 重定向不泄露 Basic Auth ----------


class _Redirector(BaseHTTPRequestHandler):
    hop_auth: list[str | None] = []
    code = 302

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 — stdlib handler name
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(self.code)
        self.send_header("Location", self.hop_url)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Hop(BaseHTTPRequestHandler):
    auth: list[str | None] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 — stdlib handler name
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        type(self).auth.append(self.headers.get("Authorization"))
        body = b'{"code":0}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_redirect_does_not_leak_basic_auth(code: int) -> None:
    hop = HTTPServer(("127.0.0.1", 0), _Hop)
    _Hop.auth.clear()
    threading.Thread(target=hop.serve_forever, daemon=True).start()

    redirector = type(
        "Redirector",
        (_Redirector,),
        {"code": code, "hop_url": f"http://127.0.0.1:{hop.server_port}/rest/sql/wind_hub"},
    )
    main = HTTPServer(("127.0.0.1", 0), redirector)
    threading.Thread(target=main.serve_forever, daemon=True).start()
    try:
        sink = TDengineSink(
            Sink(
                sink_id="td",
                kind="tdengine",
                enabled=True,
                connection={**CONNECTION, "port": main.server_port},
            )
        )
        with pytest.raises(SinkError, match="TDengine request failed: HTTPError"):
            sink._execute("SELECT 1")
        assert _Hop.auth == []  # 跳转目标从未收到请求（更无凭据）
        assert sink._error is None or "td-secret-value" not in sink._error
    finally:
        main.shutdown()
        hop.shutdown()


class _Ok(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 — stdlib handler name
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"code": 0}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_plain_http_code_zero_still_accepted() -> None:
    server = HTTPServer(("127.0.0.1", 0), _Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        sink = TDengineSink(
            Sink(
                sink_id="td",
                kind="tdengine",
                enabled=True,
                connection={**CONNECTION, "port": server.server_port},
            )
        )
        sink._execute("SELECT SERVER_VERSION()")  # 无异常即通过
    finally:
        server.shutdown()


class _Unauthorized(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802 — stdlib handler name
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"code": 1, "desc": "incorrect password"}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_error_message_does_not_leak_password() -> None:
    server = HTTPServer(("127.0.0.1", 0), _Unauthorized)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        sink = TDengineSink(
            Sink(
                sink_id="td",
                kind="tdengine",
                enabled=True,
                connection={**CONNECTION, "port": server.server_port},
            )
        )
        with pytest.raises(SinkError) as excinfo:
            sink._execute("SELECT 1")
        assert "td-secret-value" not in str(excinfo.value)
    finally:
        server.shutdown()
