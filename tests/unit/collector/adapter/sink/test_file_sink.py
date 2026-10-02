"""Unit tests for the real FileSink implementation (CSV / JSONL).

覆盖：构造期参数校验、文件/父目录创建、CSV 与 JSONL 序列化、缓冲刷盘
（``buffer_size`` / ``flush_interval`` / 显式 ``flush`` / ``close``）、
滚动（大小 / 时间 / 文件名）、幂等 open/close、以及健康状态跟踪。

所有 I/O 均落在 ``tmp_path`` 下，不触碰真实生产路径。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from wind_hub_collector.adapter.outbound.sink.file.csv import FileSink
from wind_hub_core.config.schema import SinkConfig
from wind_hub_core.model.errors import ConfigError, SinkError
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointValue, Quality

_TS = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
_CSV_HEADER = "device_id,point_id,value,quality,timestamp,source"


def _cfg(path: str, **params: Any) -> SinkConfig:
    return SinkConfig(name="s1", type="file", params={"path": path, **params})


def _pv(
    point_id: str = "p1",
    value: Any = 1.0,
    device_id: str = "d1",
    quality: Quality = Quality.GOOD,
    source: str | None = "modbus",
) -> PointValue:
    return PointValue(
        device_id=device_id,
        point_id=point_id,
        value=value,
        quality=quality,
        timestamp=_TS,
        source=source,
    )


def _raise_oserror(*args: object, **kwargs: object) -> None:
    raise OSError("permission denied")


# ---------------------------------------------------------------------------
# 构造期参数校验
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_missing_path_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="path"):
            FileSink(SinkConfig(name="s1", type="file", params={}))

    def test_empty_path_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="path"):
            FileSink(SinkConfig(name="s1", type="file", params={"path": ""}))

    def test_invalid_format_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="format"):
            FileSink(_cfg("/tmp/x.jsonl", format="xml"))

    def test_invalid_max_size_mb_raises(self) -> None:
        with pytest.raises(ConfigError, match="max_size_mb"):
            FileSink(_cfg("/tmp/x.jsonl", max_size_mb=0))

    def test_invalid_buffer_size_raises(self) -> None:
        with pytest.raises(ConfigError, match="buffer_size"):
            FileSink(_cfg("/tmp/x.jsonl", buffer_size=0))

    def test_invalid_flush_interval_raises(self) -> None:
        with pytest.raises(ConfigError, match="flush_interval"):
            FileSink(_cfg("/tmp/x.jsonl", flush_interval=-1.0))

    def test_valid_construction(self) -> None:
        sink = FileSink(_cfg("/tmp/x.jsonl"))
        assert sink.health() == HealthStatus(healthy=True)


# ---------------------------------------------------------------------------
# open / close 生命周期
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_open_creates_file(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(_cfg(str(p)))
        await sink.open()
        assert p.exists()
        await sink.close()

    async def test_open_creates_parent_dirs(self, tmp_path: Path) -> None:
        p = tmp_path / "a" / "b" / "out.jsonl"
        sink = FileSink(_cfg(str(p)))
        await sink.open()
        assert p.exists()
        assert p.parent.is_dir()
        await sink.close()

    async def test_open_close_idempotent(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(_cfg(str(p)))
        await sink.open()
        await sink.open()  # 重复 open 无副作用
        await sink.close()
        await sink.close()  # 重复 close 无副作用

    async def test_open_failure_raises_sink_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sink = FileSink(_cfg(str(tmp_path / "out.jsonl")))
        monkeypatch.setattr("builtins.open", _raise_oserror)
        with pytest.raises(SinkError, match="open failed"):
            await sink.open()

    async def test_write_before_open_raises(self, tmp_path: Path) -> None:
        sink = FileSink(_cfg(str(tmp_path / "out.jsonl")))
        with pytest.raises(SinkError, match="before open"):
            await sink.write([_pv()])



# ---------------------------------------------------------------------------
# 序列化格式
# ---------------------------------------------------------------------------


class TestFormat:
    async def test_jsonl_is_default_and_serializes_all_fields(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(_cfg(str(p)))
        await sink.open()
        await sink.write([_pv(value=800.0)])
        await sink.flush()
        await sink.close()

        data = json.loads(p.read_text().strip())
        assert data == {
            "device_id": "d1",
            "point_id": "p1",
            "value": 800.0,
            "quality": "good",
            "timestamp": "2026-09-16T12:00:00+00:00",
            "source": "modbus",
        }

    async def test_jsonl_multi_line(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(_cfg(str(p)))
        await sink.open()
        await sink.write([_pv(point_id="a"), _pv(point_id="b", value=2.0)])
        await sink.flush()
        await sink.close()

        lines = [json.loads(line) for line in p.read_text().strip().split("\n")]
        assert [line["point_id"] for line in lines] == ["a", "b"]

    async def test_csv_writes_header_and_row(self, tmp_path: Path) -> None:
        p = tmp_path / "out.csv"
        sink = FileSink(_cfg(str(p), format="csv"))
        await sink.open()
        await sink.write([_pv(value=42.5)])
        await sink.flush()
        await sink.close()

        lines = p.read_text().strip().split("\n")
        assert lines[0] == _CSV_HEADER
        assert lines[1] == "d1,p1,42.5,good,2026-09-16T12:00:00+00:00,modbus"

    async def test_csv_quotes_value_with_comma(self, tmp_path: Path) -> None:
        p = tmp_path / "out.csv"
        sink = FileSink(_cfg(str(p), format="csv"))
        await sink.open()
        await sink.write([_pv(value="a,b", source=None)])
        await sink.flush()
        await sink.close()

        row = p.read_text().strip().split("\n")[1]
        assert '"a,b"' in row

    async def test_csv_write_header_disabled(self, tmp_path: Path) -> None:
        p = tmp_path / "out.csv"
        sink = FileSink(_cfg(str(p), format="csv", write_header=False))
        await sink.open()
        await sink.write([_pv(value=1.0)])
        await sink.flush()
        await sink.close()

        lines = p.read_text().strip().split("\n")
        assert len(lines) == 1
        assert not lines[0].startswith("device_id,")


# ---------------------------------------------------------------------------
# 缓冲与刷盘
# ---------------------------------------------------------------------------


class TestFlush:
    async def test_buffer_threshold_triggers_flush(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(_cfg(str(p), buffer_size=2, flush_interval=1000))
        await sink.open()
        await sink.write([_pv(point_id="a")])  # 仅 1 行，尚未达到阈值
        assert p.read_text() == ""
        await sink.write([_pv(point_id="b")])  # 达到阈值 → 刷盘两行
        assert len(p.read_text().strip().split("\n")) == 2
        await sink.close()

    async def test_flush_forces_flush(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(_cfg(str(p), buffer_size=1000, flush_interval=1000))
        await sink.open()
        await sink.write([_pv(point_id="a")])
        assert p.read_text() == ""  # 仍缓冲中
        await sink.flush()
        assert len(p.read_text().strip().split("\n")) == 1
        await sink.close()

    async def test_close_forces_flush(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(_cfg(str(p), buffer_size=1000, flush_interval=1000))
        await sink.open()
        await sink.write([_pv(point_id="a")])
        assert p.read_text() == ""
        await sink.close()  # close 强制 flush
        assert len(p.read_text().strip().split("\n")) == 1

    async def test_flush_interval_triggers_flush(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(_cfg(str(p), buffer_size=1000, flush_interval=0.05))
        await sink.open()
        await sink.write([_pv(point_id="a")])
        await asyncio.sleep(0.1)  # 超过 flush_interval
        await sink.write([_pv(point_id="b")])  # 惰性检查触发刷盘，两行一起落盘
        assert len(p.read_text().strip().split("\n")) == 2
        await sink.close()

    async def test_write_empty_batch_is_noop(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(_cfg(str(p)))
        await sink.open()
        await sink.write([])
        assert p.read_text() == ""
        await sink.close()


# ---------------------------------------------------------------------------
# 滚动
# ---------------------------------------------------------------------------


class TestRollover:
    async def test_size_rollover(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        # max_size_mb 极小 → 首行落盘即超阈值；buffer_size=1 让每次 write 立即刷盘。
        sink = FileSink(_cfg(str(p), max_size_mb=0.000001, buffer_size=1, flush_interval=1000))
        await sink.open()
        await sink.write([_pv(point_id="a")])
        await sink.close()

        rolled = list(tmp_path.glob("out.*.jsonl"))
        assert len(rolled) == 1
        assert json.loads(rolled[0].read_text().strip())["point_id"] == "a"

    async def test_time_rollover(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(_cfg(str(p), max_age_hours=0.000001, buffer_size=1, flush_interval=1000))
        await sink.open()
        await sink.write([_pv(point_id="a")])
        assert not list(tmp_path.glob("out.*.jsonl"))  # 未超时，未滚动
        await asyncio.sleep(0.05)  # 超过 max_age
        await sink.write([_pv(point_id="b")])  # 超时 → 滚动
        await sink.close()

        rolled = list(tmp_path.glob("out.*.jsonl"))
        assert len(rolled) == 1
        assert len(rolled[0].read_text().strip().split("\n")) == 2

    async def test_rolled_filename_pattern(self, tmp_path: Path) -> None:
        p = tmp_path / "data.jsonl"
        sink = FileSink(_cfg(str(p), max_size_mb=0.000001, buffer_size=1, flush_interval=1000))
        await sink.open()
        await sink.write([_pv(point_id="x")])
        await sink.close()

        rolled = list(tmp_path.glob("data.*.jsonl"))
        assert len(rolled) == 1
        assert rolled[0].name.startswith("data.")
        assert rolled[0].name.endswith(".jsonl")

    async def test_no_rollover_without_limits(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(_cfg(str(p), buffer_size=1))
        await sink.open()
        for i in range(3):
            await sink.write([_pv(point_id=f"p{i}")])
        await sink.close()
        assert not list(tmp_path.glob("out.*.jsonl"))  # 未配置滚动，不产出归档文件
        assert len(p.read_text().strip().split("\n")) == 3


# ---------------------------------------------------------------------------
# 健康状态
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_healthy_by_default(self) -> None:
        sink = FileSink(_cfg("/tmp/x.jsonl"))
        assert sink.health() == HealthStatus(healthy=True)

    async def test_single_failure_keeps_healthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sink = FileSink(_cfg(str(tmp_path / "out.jsonl")))
        monkeypatch.setattr("builtins.open", _raise_oserror)
        with pytest.raises(SinkError):
            await sink.open()
        assert sink.health().healthy is True

    async def test_repeated_failures_mark_unhealthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sink = FileSink(_cfg(str(tmp_path / "out.jsonl")))
        monkeypatch.setattr("builtins.open", _raise_oserror)
        for _ in range(5):
            with pytest.raises(SinkError):
                await sink.open()
        assert sink.health().healthy is False
        assert "open failed" in (sink.health().message or "")
