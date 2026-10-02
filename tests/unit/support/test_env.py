"""tests/support/env.py 的单元测试——重点是 test.env 定位回归。

历史缺陷：``TEST_ENV_FILE`` 曾按 ``tests/support/test.env`` 计算，导致按
模板说明复制到 ``tests/test.env`` 的配置完全不被读取，真实服务测试被
静默 SKIPPED。首条测试直接钉死该路径契约。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import env as env_mod


@pytest.fixture
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 TEST_ENV_FILE 重定向到临时目录并复位加载缓存。"""
    target = tmp_path / "test.env"
    monkeypatch.setattr(env_mod, "TEST_ENV_FILE", target)
    monkeypatch.setattr(env_mod, "_loaded", False)
    return target


class TestEnvFileLocation:
    def test_env_file_is_tests_root_test_env(self) -> None:
        """回归：配置必须读取 tests/test.env，而非 tests/support/test.env。"""
        assert env_mod.TEST_ENV_FILE.name == "test.env"
        assert env_mod.TEST_ENV_FILE.parent.name == "tests"
        assert env_mod.TEST_ENV_FILE.parent.parent == Path(
            env_mod.__file__
        ).resolve().parents[2]

    def test_example_template_next_to_env_file(self) -> None:
        """模板 tests/test.env.example 必须与目标文件同目录存在。"""
        template = env_mod.TEST_ENV_FILE.parent / "test.env.example"
        assert template.is_file()


class TestLoadTestEnv:
    def test_loads_values_without_overriding_process_env(
        self, _isolated_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _isolated_env.write_text(
            "# comment\n"
            "\n"
            "WIND_HUB_UNIT_FROM_FILE=from-file\n"
            "WIND_HUB_UNIT_FROM_PROCESS=from-file\n"
            "malformed line without equals\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("WIND_HUB_UNIT_FROM_PROCESS", "from-process")
        monkeypatch.delenv("WIND_HUB_UNIT_FROM_FILE", raising=False)

        env_mod.load_test_env()

        assert env_mod.env_or_none("WIND_HUB_UNIT_FROM_FILE") == "from-file"
        # 进程环境变量优先于文件。
        assert env_mod.env_or_none("WIND_HUB_UNIT_FROM_PROCESS") == "from-process"

    def test_missing_file_is_noop(self, _isolated_env: Path) -> None:
        env_mod.load_test_env()  # 文件不存在——不得抛错
        assert not _isolated_env.exists()

    def test_load_is_idempotent(self, _isolated_env: Path) -> None:
        _isolated_env.write_text("WIND_HUB_UNIT_ONCE=first\n", encoding="utf-8")
        env_mod.load_test_env()
        _isolated_env.write_text("WIND_HUB_UNIT_ONCE=second\n", encoding="utf-8")
        env_mod.load_test_env()
        assert env_mod.env_or_none("WIND_HUB_UNIT_ONCE") == "first"


class TestEnvOrNone:
    def test_empty_string_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WIND_HUB_UNIT_EMPTY", "   ")
        assert env_mod.env_or_none("WIND_HUB_UNIT_EMPTY") is None

    def test_unset_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WIND_HUB_UNIT_UNSET", raising=False)
        assert env_mod.env_or_none("WIND_HUB_UNIT_UNSET") is None
