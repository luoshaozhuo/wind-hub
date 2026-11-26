"""证明 DCEMS 部署配置派生隔离，不触及发布包及原数据库连接。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.third_party.dc_ems.prepare_runtime import prepare, read_env


def _source(root: Path) -> Path:
    (root / "config" / "ssl").mkdir(parents=True)
    (root / "config" / "ssl" / "dc-ems.jks").write_bytes(b"test-fixture")
    (root / "config" / "log4j.properties").write_text("log=fixture\n", encoding="utf-8")
    (root / ".env").write_text(
        "DB_NAME=legacy\nDB_USER=postgres\nDB_PASSWORD=local-only\n"
        "NODE_ROLE=primary\nLICENSE_CODE=\nPEER_HOST=\n"
        "REDIS_PASSWORD=must-not-copy\n",
        encoding="utf-8",
    )
    return root


def test_prepared_database_is_dedicated(tmp_path: Path) -> None:
    source = _source(tmp_path / "release")
    destination = tmp_path / "runtime"
    before = (source / ".env").read_bytes()
    prepare(source, destination)
    assert (source / ".env").read_bytes() == before
    environ = (destination / "dcems.env").read_text(encoding="utf-8")
    assert "DB_NAME=dcems_smoke\n" in environ
    assert "REDIS_PASSWORD" not in environ
    assert "PEER_HOST=\n" in environ
    config = (destination / "config" / "db.property").read_text(encoding="utf-8")
    assert "jdbc:postgresql://127.0.0.1:5432/dcems_smoke?" in config
    assert "legacy" not in config
    assert (destination / "dcems.env").stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        prepare(source, destination)


def test_duplicate_environment_credentials_are_rejected(tmp_path: Path) -> None:
    source = _source(tmp_path / "release")
    with (source / ".env").open("a", encoding="utf-8") as stream:
        stream.write("DB_USER=other\n")
    with pytest.raises(ValueError, match="invalid or unknown"):
        read_env(source / ".env")
