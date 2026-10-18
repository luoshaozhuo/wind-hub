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


async def test_insert_is_batched_at_200_points(monkeypatch: pytest.MonkeyPatch) -> None:
    """大型批次应分片，不将所有点拼成单个 REST 请求。"""
    sink = make_sink()
    statements: list[str] = []
    monkeypatch.setattr(sink, "_execute", statements.append)
    await sink.open()
    await sink.write([PointValue("WT001", f"p{i}", i) for i in range(401)])
    inserts = [sql for sql in statements if sql.startswith("INSERT INTO ")]
    assert len(inserts) == 3
    assert [sql.count(" USING ") for sql in inserts] == [200, 200, 1]
    await sink.close()


async def test_value_quotes_are_escaped_in_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = make_sink()
    statements: list[str] = []
    monkeypatch.setattr(sink, "_execute", statements.append)
    await sink.open()
    await sink.write([PointValue("WT'001", "message", "a'b")])
    insert = statements[-1]
    assert "WT''001" in insert
    assert "a''b" in insert
    await sink.close()


async def test_later_insert_failure_does_not_replay_accepted_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部分 INSERT 成功后报错，Sink 不得自动重传造成重复时间序列写入。"""
    sink = make_sink()
    calls: list[str] = []

    def execute(sql: str) -> None:
        calls.append(sql)
        if sql.startswith("INSERT INTO ") and sum(
            item.startswith("INSERT INTO ") for item in calls
        ) == 2:
            raise OSError("second chunk failed")

    monkeypatch.setattr(sink, "_execute", execute)
    await sink.open()
    with pytest.raises(Exception, match="TDengine write failed"):
        await sink.write([PointValue("WT001", f"p{i}", i) for i in range(201)])

    inserts = [sql for sql in calls if sql.startswith("INSERT INTO ")]
    assert len(inserts) == 2
    assert [sql.count(" USING ") for sql in inserts] == [200, 1]
    await sink.close()


async def test_oversized_value_rejected_without_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = make_sink()
    calls: list[str] = []
    monkeypatch.setattr(sink, "_execute", calls.append)
    await sink.open()
    with pytest.raises(Exception, match="exceeds 2048"):
        await sink.write([PointValue("WT001", "message", "x" * 2049)])
    assert all(not sql.startswith("INSERT INTO ") for sql in calls)
    await sink.close()
