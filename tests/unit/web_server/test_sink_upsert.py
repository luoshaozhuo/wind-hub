"""SinkService.upsert 落盘契约：sinks.yaml 不得再写 sink 级 points。

旧实现无条件写入 ``points`` 键，而新 Schema 下 Sink 定义 extra=forbid、
点属于 Task outputs，导致 PUT /sinks/{name} 100% 校验失败并回滚。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import yaml

from core.infrastructure.config.adapter import YamlConfigAdapter
from tests.support.new_collector import write_collector_config_tree
from tests.unit.web_server.test_config import COLLECTOR_ID, _FakeDirectory, _FakeWorker
from web_server.application.config.files import ConfigFileService
from web_server.application.config.service import ConfigService
from web_server.application.sink.service import SinkService
from web_server.application.worker.model import COMMANDER_WORKER_ID


def _service(tmp_path: Path) -> tuple[SinkService, Path]:
    base = write_collector_config_tree(tmp_path / "cfg")
    collector = _FakeWorker(COLLECTOR_ID)
    commander = _FakeWorker(COMMANDER_WORKER_ID)
    directory = _FakeDirectory({COLLECTOR_ID: collector})
    config = ConfigService(
        base,
        directory,
        commander,
        ConfigService.load_directory(base),
    )
    admin = ConfigFileService(config)
    service = SinkService(
        collectors=directory,
        monitoring=SimpleNamespace(sinks_snapshot=lambda: []),
        placements=MagicMock(),
        config=config,
        admin=admin,
    )
    return service, base


async def test_upsert_writes_sink_without_points_and_applies(tmp_path: Path) -> None:
    service, base = _service(tmp_path)

    result = await service.upsert(
        "file_archive",
        {
            "type": "file",
            "enabled": True,
            "connection": {"path": "/tmp/wind-hub-upsert", "retention_days": 14},
        },
    )

    assert result.success, result.errors
    raw = yaml.safe_load((base / "sinks.yaml").read_text(encoding="utf-8"))
    entry = raw["sinks"]["file_archive"]
    assert entry == {
        "type": "file",
        "enabled": True,
        "connection": {"path": "/tmp/wind-hub-upsert", "retention_days": 14},
    }
    assert "points" not in entry
    # 落盘结果必须通过正式 Schema 重新加载（旧实现在此抛 extra-forbid）。
    snapshot = YamlConfigAdapter(base).load()
    assert snapshot.sinks["file_archive"].kind == "file"


async def test_upsert_ignores_legacy_points_in_payload(tmp_path: Path) -> None:
    """旧客户端仍携带 points 字段时，后端忽略而非落盘。"""
    service, base = _service(tmp_path)

    result = await service.upsert(
        "file_legacy",
        {
            "type": "file",
            "enabled": True,
            "connection": {"path": "/tmp/wind-hub-legacy"},
            "points": [{"ref": "ghost"}],
        },
    )

    assert result.success, result.errors
    raw = yaml.safe_load((base / "sinks.yaml").read_text(encoding="utf-8"))
    assert "points" not in raw["sinks"]["file_legacy"]
