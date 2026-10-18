"""LogStore 单元测试。"""

import logging

from web_server.infra.log_store import LogStore


def test_log_store_filters_and_bounds() -> None:
    store = LogStore(capacity=2)
    logger = logging.getLogger("web_server.test")
    for message in ("one", "two", "three"):
        record = logger.makeRecord(logger.name, logging.ERROR, __file__, 1, message, (), None)
        store.append_record(record)

    rows = store.query(level="ERROR", keyword="three")
    assert len(rows) == 1
    assert rows[0].message == "three"
    assert len(store.query()) == 2


def test_log_store_captures_request_id_from_context() -> None:
    from core.infrastructure.diagnostic.correlation import bind_request_id

    store = LogStore(capacity=5)
    logger = logging.getLogger("web_server.test.rid")
    record = logger.makeRecord(logger.name, logging.ERROR, __file__, 1, "boom", (), None)
    with bind_request_id("rid-123"):
        store.append_record(record)

    rows = store.query(keyword="rid-123")
    assert len(rows) == 1
    assert rows[0].request_id == "rid-123"


def test_log_store_defaults_request_id_to_dash() -> None:
    store = LogStore(capacity=5)
    logger = logging.getLogger("web_server.test.norid")
    record = logger.makeRecord(logger.name, logging.INFO, __file__, 1, "plain", (), None)
    store.append_record(record)

    rows = store.query()
    assert rows[0].request_id == "-"
