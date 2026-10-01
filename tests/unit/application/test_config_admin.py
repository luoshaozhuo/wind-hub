"""ConfigAdminUseCase 的最小纯文件测试。"""

from unittest.mock import MagicMock

from wind_hub.application.usecase.config_admin import CONFIG_FILES, ConfigAdminUseCase


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
