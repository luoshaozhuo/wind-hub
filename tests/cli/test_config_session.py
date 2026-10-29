"""ConfigSession 与 shell 层 config 命令契约。"""

from __future__ import annotations

import hashlib

import pytest

from cli.config_session import REQUIRED_FILES, ConfigSession
from cli.main import WindHubShell
from core.application.errors import ConfigError


def test_validate_success_does_not_replace_session(config_dir) -> None:
    base = config_dir("modbus")
    session = ConfigSession()
    loaded = session.validate(base)
    assert loaded.fingerprint
    assert session._current is None


def test_read_success_activates_snapshot_and_fingerprint_matches_bytes(config_dir) -> None:
    base = config_dir("modbus")
    session = ConfigSession()
    loaded = session.read(base)
    digest = hashlib.sha256()
    for name in REQUIRED_FILES:
        content = (base / name).read_bytes()
        digest.update(name.encode())
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    assert loaded.fingerprint == digest.hexdigest()
    assert session.current is loaded


def test_read_missing_directory(config_dir) -> None:
    base = config_dir("modbus") / "missing"
    with pytest.raises(ConfigError, match="not found"):
        ConfigSession().read(base)


def test_read_missing_required_file(config_dir) -> None:
    base = config_dir("modbus")
    (base / "sinks.yaml").unlink()
    with pytest.raises(ConfigError, match="sinks.yaml"):
        ConfigSession().read(base)


def test_read_bad_yaml_raises_config_error(config_dir) -> None:
    base = config_dir("modbus")
    (base / "points.yaml").write_text(":\n  - [unterminated\n")
    with pytest.raises(ConfigError):
        ConfigSession().read(base)


def test_read_reference_error_raises_config_error(config_dir) -> None:
    base = config_dir("modbus")
    devices = (base / "devices.yaml").read_text()
    (base / "devices.yaml").write_text(devices.replace("modbus_wtg", "missing_model"))
    with pytest.raises(ConfigError, match="unknown model"):
        ConfigSession().read(base)


def test_failed_read_keeps_previous_snapshot(config_dir) -> None:
    base = config_dir("modbus")
    session = ConfigSession()
    original = session.read(base)
    (base / "devices.yaml").write_text(": bad yaml\n")
    with pytest.raises(ConfigError):
        session.read(base)
    assert session.current is original


def test_reload_same_directory_replaces_snapshot(config_dir) -> None:
    base = config_dir("modbus")
    session = ConfigSession()
    first = session.read(base)
    second = session.reload()
    assert second is not first
    assert second.fingerprint == first.fingerprint
    assert not session.reload_failed


def test_failed_reload_keeps_snapshot_and_blocks_diagnose(config_dir) -> None:
    base = config_dir("modbus")
    session = ConfigSession()
    original = session.read(base)
    (base / "system.yaml").unlink()
    with pytest.raises(ConfigError):
        session.reload()
    assert session.reload_failed is True
    assert session.current is original
    with pytest.raises(ConfigError, match="reload failed"):
        session.require_diagnosable()
    # 恢复为有效配置后门禁解除。
    session._reload_failed = False
    assert session.require_diagnosable() is original


def test_require_diagnosable_without_loaded_config() -> None:
    with pytest.raises(ConfigError, match="not loaded"):
        ConfigSession().require_diagnosable()


def test_diff_no_changes(config_dir) -> None:
    base = config_dir("modbus")
    session = ConfigSession()
    session.read(base)
    change = session.diff(base)
    assert change.has_changes is False
    assert all(
        not section.added and not section.removed and not section.changed
        for section in change.sections.values()
    )


def test_diff_detects_change_without_switching(config_dir) -> None:
    first = config_dir("modbus")
    session = ConfigSession()
    session.read(first)
    second = config_dir("iec104")
    change = session.diff(second)
    assert change.has_changes is True
    assert change.sections
    # diff 不切换当前配置。
    assert session.current.path == first.resolve()


def test_diff_invalid_target_keeps_snapshot(config_dir) -> None:
    base = config_dir("modbus")
    session = ConfigSession()
    current = session.read(base)
    with pytest.raises(ConfigError):
        session.diff(base / "does-not-exist")
    assert session.current is current


def test_validation_rejects_files_changed_during_load(config_dir, monkeypatch) -> None:
    base = config_dir("modbus")
    from core.infrastructure.config.adapter import YamlConfigAdapter

    real_load = YamlConfigAdapter.load

    def tampering_load(self: YamlConfigAdapter) -> object:
        result = real_load(self)
        (base / "tasks.yaml").write_text((base / "tasks.yaml").read_text() + "\n# mutated\n")
        return result

    monkeypatch.setattr(YamlConfigAdapter, "load", tampering_load)
    with pytest.raises(ConfigError, match="changed during validation"):
        ConfigSession().read(base)


# ---- shell 命令与退出码 ----


def test_cli_validate_success_exit_zero(run_cli, config_dir) -> None:
    rc, out, _ = run_cli(["config", "validate", str(config_dir("modbus"))])
    assert rc == 0
    assert "PASS" in out


def test_cli_validate_missing_directory_exit_two(run_cli, tmp_path) -> None:
    rc, out, _ = run_cli(["config", "validate", str(tmp_path / "nope")])
    assert rc == 2
    assert "CONFIG_DIRECTORY_NOT_FOUND" in out


def test_cli_validate_missing_file_exit_two(run_cli, config_dir) -> None:
    base = config_dir("modbus")
    (base / "units.yaml").unlink()
    rc, out, _ = run_cli(["config", "validate", str(base)])
    assert rc == 2
    assert "CONFIG_FILE_MISSING" in out
    assert "units.yaml" in out


def test_cli_validate_invalid_yaml_exit_two(run_cli, config_dir) -> None:
    base = config_dir("modbus")
    (base / "sinks.yaml").write_text("a: [unclosed\n")
    rc, out, _ = run_cli(["config", "validate", str(base)])
    assert rc == 2
    assert "CONFIG_INVALID" in out


def test_cli_read_then_show_and_reload(run_cli, config_dir) -> None:
    base = config_dir("modbus")
    rc, _, _ = run_cli(["--config", str(base), "config", "show"])
    assert rc == 0
    rc, out, _ = run_cli(["--config", str(base), "config", "reload"])
    assert rc == 0
    assert "PASS" in out


def test_cli_failed_reload_exit_two_and_keeps_snapshot(config_dir, capsys) -> None:
    base = config_dir("modbus")
    shell = WindHubShell()
    original = shell.config.read(base)
    (base / "devices.yaml").write_text("bad: [yaml\n")
    # 交互 shell 内失败被 default 转成 FAIL 信封，不抛出、不退出进程。
    shell.default("config reload")
    printed = capsys.readouterr().out
    assert "FAIL" in printed
    assert shell.config.current is original
    show = shell.dispatch(["config", "show"])
    assert isinstance(show, dict)
    assert show["last_reload"] == "FAIL"


def test_cli_diff_no_changes(run_cli, config_dir) -> None:
    base = config_dir("modbus")
    rc, out, _ = run_cli(["--config", str(base), "config", "diff", str(base)])
    assert rc == 0
    assert '"has_changes": false' in out


def test_cli_diff_invalid_target_exit_two(run_cli, config_dir, tmp_path) -> None:
    base = config_dir("modbus")
    rc, _, err = run_cli(["--config", str(base), "config", "diff", str(tmp_path / "missing")])
    assert rc == 2
    assert "ConfigError" in err


def test_dispatch_config_usage_errors() -> None:
    shell = WindHubShell()
    with pytest.raises(ValueError, match="usage"):
        shell.dispatch(["config"])
    with pytest.raises(ValueError, match="usage"):
        shell.dispatch(["config", "read"])
    with pytest.raises(ValueError, match="unsupported"):
        shell.dispatch(["config", "frobnicate", "x"])


def test_cli_config_validate_scope_all_accepted(run_cli, config_dir) -> None:
    base = config_dir("modbus")
    rc, _, _ = run_cli(["config", "validate", str(base), "--scope", "all"])
    assert rc == 0
