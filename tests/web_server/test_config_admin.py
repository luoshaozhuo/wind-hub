"""ConfigFileService 的最小纯文件测试。"""

from unittest.mock import MagicMock

import pytest
import yaml

from web_server.application.config.files import (
    CONFIG_FILES,
    SECRET_PLACEHOLDER,
    ConfigFileService,
    redact_secret_text,
    restore_secret_text,
)

pytestmark = pytest.mark.unit


def test_config_file_set_is_stable() -> None:
    """Admin API 必须只暴露正式配置文件。"""
    assert "system.yaml" in CONFIG_FILES
    assert "sinks.yaml" in CONFIG_FILES
    assert "devices.yaml" in CONFIG_FILES
    assert "tasks.yaml" in CONFIG_FILES
    assert "reporting.yaml" not in CONFIG_FILES
    assert ".history" not in CONFIG_FILES


def test_unknown_config_file_is_rejected(tmp_path) -> None:
    config = MagicMock()
    config.config_dir = tmp_path
    admin = ConfigFileService(config)

    try:
        admin.read_file("../secret")
    except KeyError:
        pass
    else:
        raise AssertionError("path traversal must be rejected")


def test_history_starts_empty(tmp_path) -> None:
    config = MagicMock()
    config.config_dir = tmp_path
    admin = ConfigFileService(config)

    assert admin.history() == []


async def test_mutate_yaml_files_reads_under_apply_lock(tmp_path) -> None:
    """结构化 mutation 必须基于锁内最新 YAML，而不是调用方预读快照。"""
    path = tmp_path / "system.yaml"
    path.write_text("site:\n  site_id: old\nsinks: []\n", encoding="utf-8")
    config = MagicMock()
    config.config_dir = tmp_path
    admin = ConfigFileService(config)

    captured: dict[str, str] = {}

    async def fake_apply(files, *, source, comment):
        captured.update(files)
        from web_server.application.config.files import ConfigApplyResult

        return ConfigApplyResult(success=True)

    admin._apply_files_locked = fake_apply  # type: ignore[method-assign]

    def mutate(documents):
        documents["system.yaml"]["site"]["site_id"] = "new"

    result = await admin.mutate_yaml_files(
        ("system.yaml",),
        mutate,
        source="test",
        comment="mutation",
    )

    assert result.success is True
    assert "site_id: new" in captured["system.yaml"]


# ---------------------------------------------------------------------------
# 配置文件凭据脱敏（Admin API 回读不得暴露明文密码）
# ---------------------------------------------------------------------------

_SINKS_YAML = """\
sinks:
  redis_main:
    type: redis
    enabled: true
    # password 写在注释里，不应被替换：password: do-not-touch
    connection:
      host: 127.0.0.1
      port: 6379
      password: "s3cr3t"
  redis_backup:
    type: redis
    enabled: false
    connection:
      host: 10.0.0.9
      password: ''
"""


def test_redact_secret_text_masks_only_live_password_scalars() -> None:
    redacted = redact_secret_text(_SINKS_YAML)
    assert "s3cr3t" not in redacted
    assert "# password 写在注释里" in redacted
    assert "do-not-touch" in redacted
    assert f"password: {SECRET_PLACEHOLDER}" in redacted
    # 空密码保持空值，不产生无意义占位符。
    assert "password: ''" in redacted


def test_read_file_redacted_masks_secret_files_only(tmp_path) -> None:
    secret_path = tmp_path / "sinks.yaml"
    secret_path.write_text(_SINKS_YAML, encoding="utf-8")
    plain_path = tmp_path / "tasks.yaml"
    plain_path.write_text("password: literal-not-a-secret\n", encoding="utf-8")
    config = MagicMock()
    config.config_dir = tmp_path
    admin = ConfigFileService(config)

    assert "s3cr3t" not in admin.read_file_redacted("sinks.yaml")
    assert admin.read_file_redacted("tasks.yaml") == "password: literal-not-a-secret\n"


def test_restore_secret_text_roundtrip_preserves_other_edits() -> None:
    redacted = redact_secret_text(_SINKS_YAML)
    # 用户在脱敏视图里改了无关字段（host），密码行原样回传。
    edited = redacted.replace("host: 127.0.0.1", "host: 127.0.0.2")
    restored = restore_secret_text(edited, _SINKS_YAML)
    loaded = yaml.safe_load(restored)
    assert loaded["sinks"]["redis_main"]["connection"]["password"] == "s3cr3t"
    assert loaded["sinks"]["redis_main"]["connection"]["host"] == "127.0.0.2"
    # 注释原样保留。
    assert "do-not-touch" in restored


def test_restore_secret_text_rejects_unresolvable_placeholder() -> None:
    # 新建 sink 携带占位符：路径在当前文件中不存在，必须拒绝而非字面落盘。
    candidate = _SINKS_YAML.replace(
        "  redis_backup:",
        "  redis_new:\n    type: redis\n    enabled: true\n    connection:\n"
        f"      password: {SECRET_PLACEHOLDER}\n  redis_backup:",
    )
    try:
        restore_secret_text(candidate, _SINKS_YAML)
    except ValueError as exc:
        assert "redis_new" in str(exc)
    else:
        raise AssertionError("unresolved secret placeholder must be rejected")
