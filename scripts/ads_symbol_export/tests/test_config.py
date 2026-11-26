"""配置契约及配置错误先于 ADS 连接的验证。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from ads_export import cli
from ads_export.config import load_config

CONFIG = Path(__file__).resolve().parents[1] / "ads_symbol_export_v1.0.0.yaml"


@pytest.mark.parametrize("method", ["auto", "full", "chunk", "file"])
def test_supported_methods(tmp_path: Path, method: str) -> None:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    raw["READ_METHOD"] = method
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert method == load_config(path).READ_METHOD


@pytest.mark.parametrize(
    "key,value",
    [
        ("READ_METHOD", "invalid"),
        ("PLC_IP", "not-an-ip"),
        ("PLC_AMS_NET_ID", "1.2.3"),
        ("TWINCAT_VERSION", 4),
        ("PLC_AMS_PORT", 65536),
        ("ADS_TIMEOUT_MS", 0),
        ("CHUNK_SIZES", [32]),
        ("RECONNECT_ATTEMPTS", -1),
        ("FILE_SERVICE_ENABLED", "true"),
        ("schema_version", "2.0.0"),
        ("file_version", "1.0"),
        ("UNKNOWN_FIELD", True),
        ("TEXT_ENCODING", "unknown-encoding"),
        ("TPY_SEARCH_PATHS", "not-a-list"),
        ("FILE_FIND_MAX_ENTRIES", 0),
        ("FILE_READ_CHUNK", 536870913),
        ("LOG_EVERY", -1),
        ("OUTPUT_FILENAME", ""),
    ],
)
def test_invalid_config(tmp_path: Path, key: str, value: object) -> None:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    raw[key] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize("key", ["PLC_IP", "READ_METHOD", "schema_version", "CHUNK_SIZES"])
def test_missing_required_field_rejected(tmp_path: Path, key: str) -> None:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    del raw[key]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="配置字段不匹配"):
        load_config(path)


def test_chunk_method_requires_chunk_sizes(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    raw["READ_METHOD"] = "chunk"
    raw["CHUNK_SIZES"] = []
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="CHUNK_SIZES 非空"):
        load_config(path)


def test_auto_requires_at_least_one_strategy(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    raw["READ_METHOD"] = "auto"
    raw["TRY_FULL_TABLE_FIRST"] = False
    raw["CHUNK_SIZES"] = []
    raw["FILE_SERVICE_ENABLED"] = False
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="至少启用一种枚举方案"):
        load_config(path)


def test_invalid_config_does_not_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("READ_METHOD: invalid\n", encoding="utf-8")
    connect = MagicMock()
    monkeypatch.setattr(cli, "_open_connection", connect)
    monkeypatch.setattr("sys.argv", ["ads-symbol-export", "--config", str(path)])
    with pytest.raises(ValueError):
        cli.main()
    connect.assert_not_called()
