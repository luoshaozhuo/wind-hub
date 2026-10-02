"""FileSink × 真实文件系统集成测试。

被测组件是 :class:`FileSink` 本身——真实打开/写盘/缓冲/滚动/关闭，
断言直接读文件系统结果。链路级「采集 → FileSink」已由
``tests/collector/functional/test_shutdown.py`` 覆盖，不在此重复。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from wind_hub.adapter.outbound.sink.file.csv import FileSink
from wind_hub.config.schema import SinkConfig
from wind_hub.domain.model.errors import ConfigError, SinkError
from wind_hub.domain.model.point import PointValue


def _pv(point_id: str, value: object, device_id: str = "modbus-1") -> PointValue:
    return PointValue(
        device_id=device_id,
        point_id=point_id,
        value=value,
        timestamp=datetime(2026, 10, 2, 8, 0, 0, tzinfo=UTC),
        source="integration-test",
    )


def _sink(path: Path, **params: object) -> FileSink:
    return FileSink(SinkConfig(name="file", type="file", params={"path": str(path), **params}))


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestJsonlRoundtrip:
    async def test_write_flush_persists_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "out" / "data.jsonl"
        sink = _sink(path, buffer_size=1000, flush_interval=3600.0)
        await sink.open()
        await sink.write([_pv("rotor.speed", 1200.5), _pv("temp.int", 25)])

        # 未达到缓冲阈值/间隔：尚未落盘。
        assert not path.exists() or path.read_text() == ""

        await sink.flush()
        rows = _read_jsonl(path)
        assert [r["point_id"] for r in rows] == ["rotor.speed", "temp.int"]
        assert rows[0]["value"] == 1200.5
        assert rows[0]["device_id"] == "modbus-1"
        assert rows[0]["quality"] == "good"
        assert rows[0]["source"] == "integration-test"
        await sink.close()

    async def test_close_flushes_pending_buffer(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        sink = _sink(path, buffer_size=1000, flush_interval=3600.0)
        await sink.open()
        await sink.write([_pv("rotor.speed", 1.5)])
        await sink.close()
        assert len(_read_jsonl(path)) == 1

    async def test_open_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "a" / "b" / "c" / "data.jsonl"
        sink = _sink(path)
        await sink.open()
        assert path.parent.is_dir()
        await sink.close()


class TestCsvFormat:
    async def test_csv_writes_header_and_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "data.csv"
        sink = _sink(path, format="csv")
        await sink.open()
        await sink.write([_pv("rotor.speed", 1200.5), _pv("temp.int", 25)])
        await sink.close()

        rows = list(csv.reader(io.StringIO(path.read_text())))
        assert rows[0] == ["device_id", "point_id", "value", "quality", "timestamp", "source"]
        assert rows[1][1] == "rotor.speed"
        assert rows[1][2] == "1200.5"
        assert rows[2][2] == "25"


class TestRotation:
    async def test_size_rotation_produces_new_segment(self, tmp_path: Path) -> None:
        path = tmp_path / "data.jsonl"
        # 极小滚动阈值：单条点值 JSON ~150 字节，两条即触发滚动。
        sink = _sink(path, max_size_mb=0.0002, buffer_size=1)
        await sink.open()
        for i in range(6):
            await sink.write([_pv("rotor.speed", float(i))])
            await sink.flush()
        await sink.close()

        siblings = sorted(p.name for p in path.parent.iterdir())
        assert len(siblings) > 1, f"expected rotated segments, got {siblings}"
        assert "data.jsonl" in siblings


class TestFailureSemantics:
    async def test_open_on_unwritable_path_raises_sink_error(self, tmp_path: Path) -> None:
        # 父路径是普通文件：open 建目录即失败。
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        sink = _sink(blocker / "data.jsonl")
        with pytest.raises(SinkError, match="open failed"):
            await sink.open()
        # 单次失败记录错误信息但未到 unhealthy 阈值（连续 5 次才翻转）。
        assert sink.health().message is not None
        assert "open failed" in sink.health().message

    async def test_consecutive_failures_mark_unhealthy(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        sink = _sink(blocker / "data.jsonl")
        for _ in range(5):
            with pytest.raises(SinkError):
                await sink.open()
        assert sink.health().healthy is False

    async def test_write_before_open_raises(self, tmp_path: Path) -> None:
        sink = _sink(tmp_path / "data.jsonl")
        with pytest.raises(SinkError, match="before open"):
            await sink.write([_pv("rotor.speed", 1.0)])


class TestConfigValidation:
    def test_missing_path_rejected(self) -> None:
        with pytest.raises(ConfigError, match="path"):
            FileSink(SinkConfig(name="file", type="file", params={}))

    def test_invalid_format_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="format"):
            _sink(tmp_path / "x", format="parquet")

    def test_invalid_buffer_size_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="buffer_size"):
            _sink(tmp_path / "x", buffer_size=0)
