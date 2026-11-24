"""Real shipped YAML and strict parser acceptance tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cli.configuration_commands import _is_local_bootstrap
from configuration_service.application.schema_catalog import iter_schemas
from configuration_service.infrastructure.adapters.outbound.yaml.codec import (
    emit_yaml,
    parse_yaml,
    semantic_hash,
)
from configuration_service.settings import load_settings
from core.configuration.publication_identity import resolve_publication_identity
from tests.support.configuration_archive import (
    SUPERSEDED_MALFORMED,
    is_superseded_malformed,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
YAML_FILES = tuple(
    path
    for path in sorted((ROOT / "configs").rglob("*.yaml"))
    if not _is_local_bootstrap(path.relative_to(ROOT / "configs").as_posix())
    and not is_superseded_malformed(path.relative_to(ROOT / "configs").as_posix())
)
SCHEMAS = {item.config_type: item for item in iter_schemas()}


def identity_for(path: Path) -> tuple[str, str]:
    """Use the production publication identity rules for every shipped YAML."""
    relative = path.relative_to(ROOT / "configs").as_posix()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    identity = resolve_publication_identity(relative, raw)
    return identity.config_type, identity.config_id


@pytest.mark.parametrize("path", YAML_FILES, ids=lambda path: str(path.relative_to(ROOT)))
def test_shipped_yaml_strict_parse_and_lossless_header_roundtrip(path: Path) -> None:
    raw = parse_yaml(path.read_bytes(), max_bytes=64 * 1024 * 1024)
    kind, _ = identity_for(path)
    header = SCHEMAS[kind].header if kind in SCHEMAS else {}
    exported = emit_yaml(raw, header)
    reparsed = parse_yaml(exported, max_bytes=64 * 1024 * 1024)
    assert reparsed == raw
    assert semantic_hash(reparsed) == semantic_hash(raw)
    assert emit_yaml(raw, header) == exported
    assert exported.startswith(b"# wind-hub Configuration Service")
    assert b"# schema_version:" in exported
    assert b"# file_version:" in exported


@pytest.mark.parametrize("relative_path", sorted(SUPERSEDED_MALFORMED))
def test_superseded_malformed_candidates_are_rejected_by_strict_parser(
    relative_path: str,
) -> None:
    """已被后继取代的畸形历史候选仍必须被严格解析器拒绝，不可静默接受。"""
    path = ROOT / "configs" / relative_path
    with pytest.raises(ValueError, match="malformed or unsafe"):
        parse_yaml(path.read_bytes(), max_bytes=64 * 1024 * 1024)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"a: 1\na: 2\n", "duplicate key"),
        (b"a: &x [1]\nb: *x\n", "alias"),
        (b"a: !!python/object/apply:os.system [id]\n", "unsafe tag"),
        (b"a: .nan\n", "non-finite"),
        (b"a: 2020-01-01\n", "timestamp type"),
        (b"a: [1\n", "malformed"),
        (b"a: 1\n---\nb: 2\n", "multiple documents"),
        (b"- 1\n", "non-mapping root"),
        (b"\xff", "non-UTF8"),
        (b"a: *missing\n", "missing alias"),
    ],
)
def test_parser_rejects_unsafe_or_ambiguous_yaml(payload: bytes, reason: str) -> None:
    del reason
    with pytest.raises(ValueError):
        parse_yaml(payload, max_bytes=1024)


def test_parser_rejects_alias_recursion_and_size_limit() -> None:
    with pytest.raises(ValueError):
        parse_yaml(b"a: &a {b: *a}\n", max_bytes=1024)
    with pytest.raises(ValueError):
        parse_yaml(b"schema_version: 1\nfile_version: 1.0.0\n", max_bytes=10)
    with pytest.raises(ValueError):
        parse_yaml(b"", max_bytes=1024)


@pytest.mark.parametrize(
    "payload",
    [
        b"schema_version: true\nfile_version: 1.0.0\n",
        b"schema_version: 1\nfile_version: 1.0\n",
        b"schema_version: 0\nfile_version: 1.0.0\n",
        b"schema_version: 1\nfile_version: 01.0.0\n",
    ],
)
def test_parser_rejects_invalid_document_versions(payload: bytes) -> None:
    with pytest.raises(ValueError):
        parse_yaml(payload, max_bytes=1024)


def test_comments_and_scalar_types_survive_export_without_coercion() -> None:
    raw = parse_yaml(
        """schema_version: 1
file_version: 1.0.0
text: "密码: # not a comment\\n雪"
integer: 42
floating: 1.25
truth: true
falsehood: false
null_value: null
array: [1, "1", true, null]
""".encode(),
        max_bytes=1024,
    )
    assert parse_yaml(emit_yaml(raw, {}), max_bytes=4096) == raw
    assert yaml.safe_load(emit_yaml(raw, {})) == raw


def test_shipped_bootstrap_loads_from_local_yaml_without_repository() -> None:
    settings = load_settings(ROOT / "deploy/configuration/service_configuration_v1.1.0.yaml")
    assert settings.service_id == "configuration-main"
    assert settings.permissions["wind-hub-cli-admin"] == frozenset({"query", "publish", "admin"})
    assert settings.page_size == 100
    assert settings.max_workers == 16


def test_bootstrap_rejects_unknown_nested_fields(tmp_path: Path) -> None:
    source = ROOT / "deploy/configuration/service_configuration_v1.1.0.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["grpc"]["unknown_option"] = True
    path = tmp_path / "configuration.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown|invalid"):
        load_settings(path)
