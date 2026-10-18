"""File Sink 连接策略（容量/保留天数/路径形态）边界测试。"""

from typing import Any

import pytest
from pydantic import ValidationError

from core.application.errors import ConfigError
from core.infrastructure.config.sink_schema import FileSinkConnection


def test_default_limits() -> None:
    cfg = FileSinkConnection(path="./data")
    assert cfg.max_size_bytes == 100 * 1024 * 1024
    assert cfg.retention_days == 30


def test_max_size_bytes_conversion() -> None:
    cfg = FileSinkConnection(path="./data", max_size_mb=1)
    assert cfg.max_size_bytes == 1024 * 1024


@pytest.mark.parametrize(
    "options",
    [
        {"max_size_mb": 0},
        {"max_size_mb": float("inf")},
        {"retention_days": 0},
        {"path": "data.jsonl"},
        {"path": "telemetry.csv"},
        {"path": "/"},
    ],
)
def test_invalid_config(options: dict[str, object]) -> None:
    with pytest.raises((ValidationError, ConfigError)):
        params: dict[str, Any] = {"path": "./data"} | options
        FileSinkConnection(**params)


def test_relative_subdirectory_path_remains_accepted() -> None:
    """相对路径是主基线既存契约，不强制绝对路径。"""
    cfg = FileSinkConnection(path="./data")
    assert cfg.path == "./data"
