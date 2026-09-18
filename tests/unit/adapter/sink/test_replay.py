"""Unit tests for the file-replay parsing and replay logic (sink/file/replay.py).

覆盖：jsonl / csv 归档行解析为 ``PointValue``（含值类型还原）、非法行跳过、
``replay_to_sink`` 的成功/失败统计与逐条写入。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from wind_hub.adapter.outbound.sink.file.replay import parse_records, replay_to_sink
from wind_hub.domain.model.errors import SinkError
from wind_hub.domain.model.point import PointValue, Quality


def _jsonl(content: list[dict]) -> str:
    return "\n".join(json.dumps(line) for line in content) + "\n"


def _point(point_id: str, value: object = 1.0) -> PointValue:
    return PointValue(device_id="d1", point_id=point_id, value=value)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


class TestParseJsonl:
    def test_parses_valid_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "a.jsonl"
        p.write_text(
            _jsonl(
                [
                    {
                        "device_id": "d1",
                        "point_id": "rotor.speed",
                        "value": 1200.5,
                        "quality": "good",
                        "timestamp": "2026-09-16T12:00:00+00:00",
                        "source": "modbus",
                    }
                ]
            )
        )

        records = parse_records(p, "jsonl")

        assert len(records) == 1
        r = records[0]
        assert r.device_id == "d1"
        assert r.point_id == "rotor.speed"
        assert r.value == 1200.5
        assert r.quality is Quality.GOOD
        assert r.source == "modbus"
        assert r.timestamp == datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)

    def test_skips_invalid_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "a.jsonl"
        p.write_text(
            '{"device_id": "d1", "point_id": "ok", "value": 1.0, '
            '"quality": "good", "timestamp": "2026-09-16T12:00:00+00:00"}\n'
            "not-json\n"
        )
        records = parse_records(p, "jsonl")
        assert [r.point_id for r in records] == ["ok"]

    def test_skips_missing_fields(self, tmp_path: Path) -> None:
        p = tmp_path / "a.jsonl"
        p.write_text('{"device_id": "d1"}\n')
        assert parse_records(p, "jsonl") == []

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "a.jsonl"
        p.write_text(
            "\n"
            '{"device_id": "d1", "point_id": "p", "value": 1, '
            '"quality": "good", "timestamp": "2026-09-16T12:00:00+00:00"}\n'
            "\n"
        )
        assert len(parse_records(p, "jsonl")) == 1


class TestParseCsv:
    def test_parses_valid_row_with_value_coercion(self, tmp_path: Path) -> None:
        p = tmp_path / "a.csv"
        p.write_text(
            "device_id,point_id,value,quality,timestamp,source\n"
            "d1,rotor.speed,1200.5,good,2026-09-16T12:00:00+00:00,modbus\n"
            "d1,count,42,good,2026-09-16T12:00:00+00:00,\n"
        )
        records = parse_records(p, "csv")
        assert len(records) == 2
        assert records[0].value == 1200.5
        assert records[0].source == "modbus"
        assert records[1].value == 42
        assert records[1].source is None

    def test_coerces_bool_and_none(self, tmp_path: Path) -> None:
        p = tmp_path / "a.csv"
        p.write_text(
            "device_id,point_id,value,quality,timestamp,source\n"
            "d1,flag,True,good,2026-09-16T12:00:00+00:00,\n"
            "d1,empty,,good,2026-09-16T12:00:00+00:00,\n"
        )
        records = parse_records(p, "csv")
        assert records[0].value is True
        assert records[1].value is None

    def test_skips_invalid_rows(self, tmp_path: Path) -> None:
        p = tmp_path / "a.csv"
        p.write_text(
            "device_id,point_id,value,quality,timestamp,source\n"
            "d1,p,1.0,good,2026-09-16T12:00:00+00:00,\n"
            "d1,p,1.0,good,not-a-timestamp,\n"
        )
        records = parse_records(p, "csv")
        assert [r.value for r in records] == [1.0]


# ---------------------------------------------------------------------------
# 重放
# ---------------------------------------------------------------------------


def _mock_sink() -> MagicMock:
    sink = MagicMock()
    sink.write = AsyncMock()
    return sink


class TestReplayToSink:
    async def test_writes_every_record_and_counts_success(self) -> None:
        sink = _mock_sink()
        records = [_point("a"), _point("b", value=2.0)]
        stats = await replay_to_sink(sink, records, rate=None)
        assert stats.total == 2
        assert stats.success == 2
        assert stats.failed == 0
        assert stats.elapsed_seconds >= 0
        assert sink.write.call_count == 2

    async def test_counts_write_failures_and_continues(self) -> None:
        sink = MagicMock()

        async def _flaky(batch: list[PointValue]) -> None:
            if batch[0].point_id == "bad":
                raise SinkError("boom")

        sink.write = AsyncMock(side_effect=_flaky)
        records = [_point("bad"), _point("good", value=2.0)]
        stats = await replay_to_sink(sink, records, rate=None)
        assert stats.total == 2
        assert stats.success == 1
        assert stats.failed == 1
        assert sink.write.call_count == 2

    async def test_rate_limiting_still_writes_all_records(self) -> None:
        sink = _mock_sink()
        records = [_point(f"p{i}", value=i) for i in range(3)]
        stats = await replay_to_sink(sink, records, rate=1000)
        assert stats.total == 3
        assert stats.success == 3

    async def test_empty_records(self) -> None:
        sink = _mock_sink()
        stats = await replay_to_sink(sink, [], rate=None)
        assert stats.total == 0
        assert stats.success == 0
        assert stats.failed == 0
        sink.write.assert_not_called()
