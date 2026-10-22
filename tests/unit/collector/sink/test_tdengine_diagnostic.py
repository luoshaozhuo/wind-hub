"""PR#92 Round5 D6：TDengine 只读诊断不得污染历史数据。

覆盖：
- TDengineSink.diagnostic 只执行 SELECT，成功/失败语义正确；
- 连续诊断不产生 INSERT/CREATE，不创建历史子表、不增加点值；
- CollectorSinkService.write_test_sink 对 TDengine 走只读诊断并明确文案，
  对没有 diagnostic 接口的 Sink（Redis/Modbus）仅检查 health，不写入数据。

无真实 taosAdapter：SQL 经替换 ``_execute`` 捕获；失败路径用抛错桩模拟。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from collector.application.services import CollectorSinkService
from core.application.sink_contract import ResolvedSink
from core.application.sink_error import SinkError
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


def _make_sink(execute: Callable[[str], None] | None = None) -> tuple[TDengineSink, list[str]]:
    config = ResolvedSink(
        sink_id="td",
        kind="tdengine",
        enabled=True,
        connection=CONNECTION,
        points=(),
        task_sources={"t1": (("dev1", "p1"),)},
    )
    sink = TDengineSink(config)
    statements: list[str] = []
    recorder: Callable[[str], None] = statements.append
    sink._execute = execute if execute is not None else recorder  # type: ignore[method-assign, assignment]
    sink._opened = True
    sink._error = None
    return sink, statements


# ---------- TDengineSink.diagnostic 本体 ----------


async def test_diagnostic_runs_only_select_query() -> None:
    sink, statements = _make_sink()
    await sink.diagnostic()
    assert statements == ["SELECT SERVER_VERSION()"]
    assert not any(sql.lstrip().upper().startswith(("INSERT", "CREATE")) for sql in statements)


async def test_repeated_diagnostics_never_insert() -> None:
    sink, statements = _make_sink()
    for _ in range(5):
        await sink.diagnostic()
    assert statements == ["SELECT SERVER_VERSION()"] * 5
    assert not any("USING wind_hub.point_values" in sql for sql in statements)
    assert not any("TAGS" in sql for sql in statements)  # 不创建历史子表


@pytest.mark.parametrize(
    "failure",
    [
        "HTTP Error 401: Unauthorized",  # 认证失败
        "<urlopen error [Errno 101] Network is unreachable>",  # 网络不可达
        "TDengine Error: invalid SQL",  # SQL 查询失败
    ],
)
async def test_diagnostic_fails_when_query_fails(failure: str) -> None:
    def execute(sql: str) -> None:
        raise RuntimeError(failure)

    sink, _ = _make_sink(execute=execute)
    with pytest.raises(SinkError, match=re.escape(failure)):
        await sink.diagnostic()
    # 失败后健康视图保留根因，后续写诊断仍 fail-closed
    health = await sink.health()
    assert health.healthy is False
    assert failure in (health.message or "")


async def test_diagnostic_fails_before_open() -> None:
    sink, statements = _make_sink()
    sink._opened = False
    with pytest.raises(SinkError, match="not opened"):
        await sink.diagnostic()
    assert statements == []


# ---------- CollectorSinkService.write_test_sink 分发 ----------


def _service(sink: object) -> CollectorSinkService:
    runtime = SimpleNamespace(sinks={"target": sink})
    return CollectorSinkService(runtime)  # type: ignore[arg-type]


async def test_service_tdengine_uses_readonly_diagnostic() -> None:
    sink, statements = _make_sink()
    result = await _service(sink).write_test_sink("target")
    assert result.success is True
    assert "未写入" in result.message
    assert statements == ["SELECT SERVER_VERSION()"]


async def test_service_tdengine_diagnostic_failure_reported() -> None:
    def execute(sql: str) -> None:
        raise RuntimeError("HTTP Error 401: Unauthorized")

    sink, _ = _make_sink(execute=execute)
    result = await _service(sink).write_test_sink("target")
    assert result.success is False
    assert "401" in result.message


async def test_service_unknown_sink_raises_keyerror() -> None:
    sink, _ = _make_sink()
    with pytest.raises(KeyError):
        await _service(sink).write_test_sink("missing")


class _HealthOnlySink:
    """无专用 diagnostic 的 Sink 仅进行健康检查。"""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.written = False

    async def health(self):
        from core.application.port.protocol import ConnectionHealth

        return ConnectionHealth(healthy=self.healthy, message="" if self.healthy else "offline")

    async def write(self, batch: list[object]) -> None:
        self.written = True


async def test_service_without_diagnostic_checks_health_without_writing() -> None:
    sink = _HealthOnlySink()
    result = await _service(sink).write_test_sink("target")
    assert result.success is True
    assert "未写入" in result.message
    assert not sink.written


async def test_service_without_diagnostic_reports_unhealthy() -> None:
    sink = _HealthOnlySink(healthy=False)
    result = await _service(sink).write_test_sink("target")
    assert result.success is False
    assert "offline" in result.message
    assert not sink.written
