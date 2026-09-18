"""端到端测试 —— File Sink 归档 → 回放（round-trip）。

驱动真实 FileSink 落盘，再用 ``parse_records`` 读回、``replay_to_sink`` 重放到
另一个独立的 FileSink 实例，验证两种格式（jsonl / csv）都能无损还原
``PointValue``（含 timestamp、quality、source、值类型）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wind_hub.adapter.outbound.sink.file.csv import FileSink
from wind_hub.adapter.outbound.sink.file.replay import parse_records, replay_to_sink
from wind_hub.config.schema import SinkConfig
from wind_hub.domain.model.point import PointValue, Quality


def _cfg(path: str, **params: Any) -> SinkConfig:
    return SinkConfig(name="s1", type="file", params={"path": path, **params})


async def test_replay_jsonl_roundtrip(tmp_path: Path) -> None:
    src_path = tmp_path / "archive.jsonl"
    dst_path = tmp_path / "replayed.jsonl"

    src = FileSink(_cfg(str(src_path), format="jsonl"))
    await src.open()
    await src.write(
        [
            PointValue(device_id="d1", point_id="a", value=1.5, source="modbus"),
            PointValue(device_id="d1", point_id="b", value=2, source="modbus"),
        ]
    )
    await src.close()

    records = parse_records(src_path, "jsonl")
    assert [(r.point_id, r.value) for r in records] == [("a", 1.5), ("b", 2)]

    dst = FileSink(_cfg(str(dst_path), format="jsonl"))
    await dst.open()
    stats = await replay_to_sink(dst, records, rate=None)
    await dst.close()

    assert stats.total == 2
    assert stats.success == 2
    assert stats.failed == 0

    replayed = parse_records(dst_path, "jsonl")
    assert [(r.point_id, r.value, r.source) for r in replayed] == [
        ("a", 1.5, "modbus"),
        ("b", 2, "modbus"),
    ]


async def test_replay_csv_roundtrip(tmp_path: Path) -> None:
    src_path = tmp_path / "archive.csv"
    dst_path = tmp_path / "replayed.csv"

    src = FileSink(_cfg(str(src_path), format="csv"))
    await src.open()
    await src.write(
        [
            PointValue(device_id="d1", point_id="rotor.speed", value=1200.5, source="modbus"),
            PointValue(device_id="d1", point_id="gen.power", value=800.0, quality=Quality.GOOD),
        ]
    )
    await src.close()

    records = parse_records(src_path, "csv")
    assert [(r.point_id, r.value) for r in records] == [
        ("rotor.speed", 1200.5),
        ("gen.power", 800.0),
    ]

    dst = FileSink(_cfg(str(dst_path), format="csv"))
    await dst.open()
    stats = await replay_to_sink(dst, records, rate=None)
    await dst.close()

    assert stats.total == 2
    assert stats.success == 2

    replayed = parse_records(dst_path, "csv")
    assert [(r.point_id, r.value, r.quality) for r in replayed] == [
        ("rotor.speed", 1200.5, Quality.GOOD),
        ("gen.power", 800.0, Quality.GOOD),
    ]
