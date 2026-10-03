"""ConfigAdminUseCase 的最小纯文件测试。"""

from unittest.mock import MagicMock

from wind_hub_server.application.usecase.config_admin import CONFIG_FILES, ConfigAdminUseCase


def test_config_file_set_is_stable() -> None:
    """Admin API 必须只暴露正式配置文件。"""
    assert "system.yaml" in CONFIG_FILES
    assert "devices.yaml" in CONFIG_FILES
    assert "tasks.yaml" in CONFIG_FILES
    assert ".history" not in CONFIG_FILES


def test_unknown_config_file_is_rejected(tmp_path) -> None:
    config = MagicMock()
    config.config_dir = tmp_path
    admin = ConfigAdminUseCase(config)

    try:
        admin.read_file("../secret")
    except KeyError:
        pass
    else:
        raise AssertionError("path traversal must be rejected")

def test_history_starts_empty(tmp_path) -> None:
    config = MagicMock()
    config.config_dir = tmp_path
    admin = ConfigAdminUseCase(config)

    assert admin.history() == []


async def test_mutate_yaml_files_reads_under_apply_lock(tmp_path) -> None:
    """结构化 mutation 必须基于锁内最新 YAML，而不是调用方预读快照。"""
    path = tmp_path / "system.yaml"
    path.write_text("site:\n  site_id: old\nsinks: []\n", encoding="utf-8")
    config = MagicMock()
    config.config_dir = tmp_path
    admin = ConfigAdminUseCase(config)

    captured: dict[str, str] = {}

    async def fake_apply(files, *, source, comment):
        captured.update(files)
        from wind_hub_server.application.usecase.config_admin import ConfigApplyResult
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
