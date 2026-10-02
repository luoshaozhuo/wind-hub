"""LogStore 单元测试。"""

import logging

from wind_hub_server.infra.log_store import LogStore


def test_log_store_filters_and_bounds() -> None:
    store = LogStore(capacity=2)
    logger = logging.getLogger("wind_hub_collector.test")
    for message in ("one", "two", "three"):
        record = logger.makeRecord(
            logger.name, logging.ERROR, __file__, 1, message, (), None
        )
        store.append_record(record)

    rows = store.query(level="ERROR", keyword="three")
    assert len(rows) == 1
    assert rows[0].message == "three"
    assert len(store.query()) == 2
