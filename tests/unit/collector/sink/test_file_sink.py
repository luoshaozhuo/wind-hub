"""新 Collector FileSink / 滚动 / 压缩单元测试（真实 tmp 文件系统）。"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from collector.application.errors import SinkError
from collector.domain.point_value import PointValue
from collector.infrastructure.sink.file.compression import (
    GzipCompressor,
    NoCompressor,
)
from collector.infrastructure.sink.file.csv import FileSink
from collector.infrastructure.sink.file.rotation import (
    CompositeRotation,
    NoRotation,
    SizeRotation,
    TimeRotation,
    build_rotation,
)
from core.application.sink_config import FileSinkConnection, SinkConfig


def _config(path: Path, **conn: object) -> SinkConfig:
    return SinkConfig(
        name="f",
        type="file",
        connection=FileSinkConnection(path=str(path), **conn),  # type: ignore[arg-type]
        points=[],
    )


def _value(n: float = 1.0) -> PointValue:
    return PointValue(device_id="d", point_id="p", value=n, source="modbus")


# ---------------------------------------------------------------------------
# FileSink
# ---------------------------------------------------------------------------


async def test_jsonl_write_and_flush(tmp_path: Path) -> None:
    sink = FileSink(_config(tmp_path / "out.jsonl"))
    await sink.open()
    await sink.write([_value(3.5)])
    await sink.flush()
    await sink.close()

    line = (tmp_path / "out.jsonl").read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert row["device_id"] == "d"
    assert row["point_id"] == "p"
    assert row["value"] == 3.5
    assert row["quality"] == "good"
    assert row["source"] == "modbus"


async def test_csv_writes_header_and_rows(tmp_path: Path) -> None:
    sink = FileSink(_config(tmp_path / "out.csv", format="csv"))
    await sink.open()
    await sink.write([_value(2.0)])
    await sink.close()

    lines = (tmp_path / "out.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "device_id,point_id,value,quality,timestamp,source"
    assert lines[1].startswith("d,p,2.0,good,")


async def test_write_before_open_raises_sink_error(tmp_path: Path) -> None:
    sink = FileSink(_config(tmp_path / "out.jsonl"))
    with pytest.raises(SinkError, match="before open"):
        await sink.write([_value()])


async def test_open_close_idempotent(tmp_path: Path) -> None:
    sink = FileSink(_config(tmp_path / "out.jsonl"))
    await sink.open()
    await sink.open()
    await sink.close()
    await sink.close()
    assert sink.health().healthy


async def test_empty_batch_is_noop(tmp_path: Path) -> None:
    sink = FileSink(_config(tmp_path / "out.jsonl"))
    await sink.open()
    await sink.write([])
    await sink.close()
    assert (tmp_path / "out.jsonl").read_text(encoding="utf-8") == ""


async def test_size_rotation_rolls_and_compresses(tmp_path: Path) -> None:
    active = tmp_path / "roll.jsonl"
    sink = FileSink(_config(active, max_size_mb=1e-9, compress=True, buffer_size=1))
    await sink.open()
    await sink.write([_value(1.0)])
    await sink.close()  # close 等待后台压缩完成

    assert active.exists()  # 滚动后重开的新活动文件
    gz_files = list(tmp_path.glob("roll.*.jsonl.gz"))
    assert len(gz_files) == 1
    with gzip.open(gz_files[0], "rt", encoding="utf-8") as fh:
        assert json.loads(fh.read().strip())["value"] == 1.0


# ---------------------------------------------------------------------------
# Rotation policy
# ---------------------------------------------------------------------------


def test_build_rotation_none() -> None:
    assert isinstance(build_rotation(None, None), NoRotation)


def test_build_rotation_single_and_composite() -> None:
    assert isinstance(build_rotation(1.0, None), SizeRotation)
    assert isinstance(build_rotation(None, 2.0), TimeRotation)
    assert isinstance(build_rotation(1.0, 2.0), CompositeRotation)


def test_size_rotation_threshold() -> None:
    policy = SizeRotation(1.0)
    assert not policy.should_rotate(1024 * 1024 - 1, 0.0)
    assert policy.should_rotate(1024 * 1024, 0.0)


def test_time_rotation_threshold() -> None:
    policy = TimeRotation(1.0)
    assert not policy.should_rotate(0, 3599.0)
    assert policy.should_rotate(0, 3600.0)


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------


def test_gzip_compressor_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "a.jsonl"
    src.write_text('{"v": 1}\n', encoding="utf-8")
    target = GzipCompressor(level=1).compress(src)
    assert target == tmp_path / "a.jsonl.gz"
    assert not src.exists()
    with gzip.open(target, "rt", encoding="utf-8") as fh:
        assert fh.read() == '{"v": 1}\n'


def test_no_compressor_returns_path(tmp_path: Path) -> None:
    src = tmp_path / "a.jsonl"
    src.touch()
    assert NoCompressor().compress(src) == src
