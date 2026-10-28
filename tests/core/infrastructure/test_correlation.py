"""core Request-ID 原语与日志配置测试。"""

from __future__ import annotations

import io
import logging

import pytest

from core.infrastructure.diagnostic.correlation import (
    RequestIdFilter,
    bind_request_id,
    get_request_id,
    new_request_id,
    normalize_request_id,
    request_id_var,
)
from core.infrastructure.diagnostic.log_setup import configure_logging


def test_normalize_strips_truncates_and_rejects_headers() -> None:
    assert normalize_request_id("  abc  ") == "abc"
    assert normalize_request_id(None) == ""
    assert normalize_request_id("") == ""
    assert normalize_request_id("a\tb") == ""
    assert normalize_request_id("a\nb") == ""
    assert normalize_request_id("x" * 200) == "x" * 128


def test_bind_request_id_restores_outer_context() -> None:
    token = request_id_var.set("outer")
    try:
        with bind_request_id("inner"):
            assert get_request_id() == "inner"
        assert get_request_id() == "outer"
    finally:
        request_id_var.reset(token)
    assert get_request_id() == ""


def test_new_request_id_is_unique_hex() -> None:
    first = new_request_id()
    second = new_request_id()
    assert first != second
    assert len(first) == 32
    assert all(char in "0123456789abcdef" for char in first)


def test_filter_uses_context_or_dash() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", (), None)
    RequestIdFilter().filter(record)
    assert record.request_id == "-"

    with bind_request_id("trace-1"):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", (), None)
        assert RequestIdFilter().filter(record) is True
        assert record.request_id == "trace-1"


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_formats_request_id() -> None:
    stream = io.StringIO()

    class _FixedStreamHandler(logging.StreamHandler):
        def __init__(self, stream: io.StringIO) -> None:
            super().__init__(stream)

    configure_logging()
    root = logging.getLogger()
    handler = _FixedStreamHandler(stream)
    handler.formatter = root.handlers[0].formatter
    handler.addFilter(RequestIdFilter())

    with bind_request_id("trace-77"):
        handler.handle(
            logging.LogRecord("demo.logger", logging.INFO, __file__, 1, "hello", (), None)
        )
    line = stream.getvalue()
    assert "[trace-77]" in line
    assert "demo.logger: hello" in line
    assert line.endswith("\n")


@pytest.fixture()
def restore_logging() -> object:
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield None
    logging.basicConfig(handlers=saved_handlers, level=saved_level, force=True)
