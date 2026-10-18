"""TDengine Sink 快速契约测试：无需真实数据库。"""

from __future__ import annotations

import pytest

from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.sink.tdengine import TDengineSink, _table_name


def make_sink() -> TDengineSink:
    return TDengineSink(
        Sink(
            sink_id="history",
            kind="tdengine",
            enabled=True,
            connection={
                "host": "localhost",
                "port": 6041,
                "database": "wind_hub",
                "stable": "point_values",
                "username": "admin",
                "password": "local-test",
            },
        )
    )


async def test_open_and_batch_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = make_sink()
    statements: list[str] = []

    def execute(sql: str) -> None:
        statements.append(sql)

    monkeypatch.setattr(sink, "_execute", execute)
    await sink.open()
    await sink.write([
        PointValue("WT001", "p", 2.5),
        PointValue("WT001", "q", 3.5),
    ])
    assert statements[0].startswith("CREATE STABLE IF NOT EXISTS")
    assert statements[1].startswith("INSERT INTO")
    assert statements[1].count(" USING ") == 2
    assert (await sink.health()).healthy
    await sink.close()


async def test_write_before_open_rejected() -> None:
    sink = make_sink()
    with pytest.raises(Exception, match="opened"):
        await sink.write([PointValue("WT001", "p", 1)])


def test_child_table_identifier_stable_and_disambiguated() -> None:
    assert _table_name("WT001", "p") == _table_name("WT001", "p")
    assert _table_name("WT001", "p") != _table_name("WT002", "p")
    assert _table_name("a:b", "c") != _table_name("a", "b:c")
