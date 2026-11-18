"""Configuration bootstrap token rules and secure startup admission."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from configuration_service.main import _load_actor_tokens
from configuration_service.settings import load_settings

ROOT = Path(__file__).resolve().parents[3]


def _bootstrap(tmp_path: Path) -> Path:
    source = ROOT / "deploy/configuration/service_configuration_v1.1.0.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["repository"]["sqlite_path"] = str(tmp_path / "configuration.sqlite3")
    raw["repository"]["backup_root"] = str(tmp_path / "backups")
    raw["security"]["cursor_secret_file"] = str(tmp_path / "cursor.secret")
    output = tmp_path / "bootstrap.yaml"
    output.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return output


def test_bootstrap_uses_token_sources_without_tls(tmp_path: Path) -> None:
    settings = load_settings(_bootstrap(tmp_path))
    assert settings.nats_subject == "wh.config.changed"
    assert settings.nats_servers == ("nats://127.0.0.1:4222",)
    assert "wind-hub-cli-admin" in settings.token_sources
    assert not hasattr(settings, "ca_file")
    assert not hasattr(settings, "cert_file")


def test_bootstrap_rejects_legacy_tls_and_missing_token_identity(tmp_path: Path) -> None:
    path = _bootstrap(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["grpc"]["tls"] = {"ca_file": "/etc/obsolete.pem"}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="grpc"):
        load_settings(path)
    del raw["grpc"]["tls"]
    del raw["security"]["token_sources"]["wind-hub-cli-admin"]
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="token source"):
        load_settings(path)


def test_environment_tokens_are_required_and_unique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(_bootstrap(tmp_path))
    for variable in settings.token_sources.values():
        monkeypatch.delenv(variable, raising=False)
    with pytest.raises(ValueError, match="missing or invalid token"):
        _load_actor_tokens(settings)
    for index, variable in enumerate(settings.token_sources.values()):
        monkeypatch.setenv(variable, f"configuration-secret-{index:02d}-with-sufficient-entropy")
    tokens = _load_actor_tokens(settings)
    assert set(tokens) == set(settings.permissions)
    first, second = list(settings.token_sources.values())[:2]
    monkeypatch.setenv(second, os.environ[first])
    with pytest.raises(ValueError, match="duplicate"):
        _load_actor_tokens(settings)
