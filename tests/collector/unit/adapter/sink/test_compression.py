"""Unit tests for the compression abstraction and FileSink gzip integration.

覆盖：``GzipCompressor`` 的往返压缩（写 ``{path}.gz``、删原文件、解压还原）、
``NoCompressor`` 原样返回、``compress_level`` 参数校验，以及 FileSink 滚动后
在后台 task 里压缩旧分片的端到端行为（``close`` 后 ``.gz`` 存在且原文件被删除）。
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import pytest

from wind_hub.adapter.outbound.sink.file.compression import GzipCompressor, NoCompressor
from wind_hub.adapter.outbound.sink.file.csv import FileSink
from wind_hub_core.config.schema import SinkConfig
from wind_hub_core.model.errors import ConfigError
from wind_hub_core.model.point import PointValue, Quality


def _cfg(path: str, **params: Any) -> SinkConfig:
    return SinkConfig(name="s1", type="file", params={"path": path, **params})


def _pv(value: float = 1.0) -> PointValue:
    return PointValue(device_id="d1", point_id="p1", value=value, quality=Quality.GOOD)


class TestGzipCompressor:
    def test_compress_roundtrip(self, tmp_path: Path) -> None:
        source = tmp_path / "data.jsonl"
        content = b'{"device_id": "d1"}\n'
        source.write_bytes(content)

        result = GzipCompressor(level=6).compress(source)

        assert result == Path(str(source) + ".gz")
        assert result.exists()
        assert not source.exists()  # 原文件被删除
        with gzip.open(result, "rb") as f:
            assert f.read() == content

    def test_level_is_passed_to_gzip(self, tmp_path: Path) -> None:
        source = tmp_path / "data.jsonl"
        source.write_bytes(b"payload")
        # 不同的 level 也能正常解压；此处只验证构造不报错 + 文件产出。
        result = GzipCompressor(level=9).compress(source)
        with gzip.open(result, "rb") as f:
            assert f.read() == b"payload"


class TestNoCompressor:
    def test_returns_same_path(self, tmp_path: Path) -> None:
        source = tmp_path / "data.jsonl"
        source.write_text("x")
        assert NoCompressor().compress(source) == source
        assert source.exists()


class TestFileSinkCompression:
    async def test_rollover_compresses_old_fragment(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(
            _cfg(
                str(p),
                compress=True,
                max_size_mb=0.000001,
                buffer_size=1,
                flush_interval=1000,
            )
        )
        await sink.open()
        await sink.write([_pv()])  # 首行即触发滚动 → 后台压缩
        await sink.close()  # close 等待压缩完成

        gz_files = list(tmp_path.glob("out.*.jsonl.gz"))
        assert len(gz_files) == 1
        with gzip.open(gz_files[0], "rb") as f:
            payload = f.read().decode("utf-8")
        assert payload.startswith('{"device_id": "d1"')
        # 未被压缩的中间名文件已删除
        assert not list(tmp_path.glob("out.*.jsonl"))

    async def test_no_compression_without_flag(self, tmp_path: Path) -> None:
        p = tmp_path / "out.jsonl"
        sink = FileSink(_cfg(str(p), max_size_mb=0.000001, buffer_size=1, flush_interval=1000))
        await sink.open()
        await sink.write([_pv()])
        await sink.close()

        assert list(tmp_path.glob("out.*.jsonl"))  # 原文归档，无 .gz
        assert not list(tmp_path.glob("out.*.jsonl.gz"))

    async def test_compression_failure_does_not_break_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """压缩失败仅记录日志，写入流程不受影响（新文件仍正常打开）。"""
        p = tmp_path / "out.jsonl"
        sink = FileSink(
            _cfg(
                str(p),
                compress=True,
                max_size_mb=0.000001,
                buffer_size=1,
                flush_interval=1000,
            )
        )

        def _boom(path: Path) -> Path:
            raise OSError("compression exploded")

        monkeypatch.setattr(GzipCompressor, "compress", _boom)
        await sink.open()
        await sink.write([_pv()])
        await sink.close()
        # 滚动照常发生，新文件可继续写；压缩失败被吞并记录日志
        assert p.exists()
        assert sink.health().healthy is True

    def test_invalid_compress_level_raises(self) -> None:
        with pytest.raises(ConfigError, match="compress_level"):
            FileSink(_cfg("/tmp/x.jsonl", compress=True, compress_level=0))

    def test_invalid_compress_level_type_raises(self) -> None:
        with pytest.raises(ConfigError, match="compress_level"):
            FileSink(_cfg("/tmp/x.jsonl", compress=True, compress_level="fast"))
