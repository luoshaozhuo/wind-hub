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
    assert load_config(path).READ_METHOD == method


@pytest.mark.parametrize(
    "key,value",
    [
        ("READ_METHOD", "invalid"), ("PLC_IP", "not-an-ip"),
        ("PLC_AMS_NET_ID", "1.2.3"), ("TWINCAT_VERSION", 4),
        ("PLC_AMS_PORT", 65536), ("ADS_TIMEOUT_MS", 0),
        ("CHUNK_SIZES", [32]), ("RECONNECT_ATTEMPTS", -1),
        ("FILE_SERVICE_ENABLED", "true"), ("schema_version", "2.0.0"),
        ("UNKNOWN_FIELD", True), ("TEXT_ENCODING", "unknown-encoding"),
    ],
)
def test_invalid_config(tmp_path: Path, key: str, value: object) -> None:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    raw[key] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_invalid_config_does_not_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("READ_METHOD: invalid\n", encoding="utf-8")
    connect = MagicMock()
    monkeypatch.setattr(cli, "_open_connection", connect)
    monkeypatch.setattr("sys.argv", ["ads-symbol-export", "--config", str(path)])
    with pytest.raises(ValueError):
        cli.main()
    connect.assert_not_called()
