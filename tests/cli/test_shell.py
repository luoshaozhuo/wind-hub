"""Shell：Help 层级、命令解析、参数错误、补全与写命令历史清理。"""

from __future__ import annotations

import pytest

from cli.help import COMMANDS, completion_words, help_text
from cli.main import WindHubShell


def test_help_registry_covers_public_vocabulary() -> None:
    assert "config validate PATH" in help_text(["config", "validate"])
    assert "Operations: check, read, read-many, write, listen, gi, benchmark" in help_text(
        ["diagnose", "protocol"]
    )
    assert "read-many" in help_text(["diagnose", "protocol", "read-many"])
    # 最长前缀回退；未知命令回退总帮助。
    assert (
        help_text(["diagnose", "protocol", "write"]) is COMMANDS[("diagnose", "protocol", "write")]
    )
    assert help_text(["frobnicate"]) == help_text([])


def test_completion_contains_command_words_and_dispatch_help() -> None:
    words = completion_words()
    for expected in ("config", "devices", "points", "sinks", "gateways", "diagnose"):
        assert expected in words
    shell = WindHubShell()
    assert shell.dispatch(["help"]) is None
    assert shell.dispatch(["diagnose", "protocol", "--help"]) is None
    assert shell.dispatch(["--help"]) is None


def test_unknown_command_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown command"):
        WindHubShell().dispatch(["nonsense"])


def test_do_exit_and_eof_stop_loop() -> None:
    shell = WindHubShell()
    assert shell.do_exit("") is True
    assert shell.do_quit("") is True
    assert shell.do_EOF("") is True
    assert shell.emptyline() is False


def test_argument_errors_do_not_raise_systemexit_for_protocol_options(config_dir) -> None:
    # 协议选项错误走 ValueError（shell 可捕获），不得 SystemExit 杀死整个 shell。
    shell = WindHubShell()
    shell.config.read(config_dir("modbus"))
    with pytest.raises(ValueError):
        shell.dispatch("diagnose protocol read modbus-001 point --bogus".split())
    with pytest.raises(ValueError):
        shell.dispatch("diagnose protocol write modbus-001".split())
    with pytest.raises(ValueError):
        shell.dispatch("diagnose protocol gi modbus-001 extra-point".split())
    with pytest.raises(ValueError):
        shell.dispatch(["diagnose", "report"])  # 无配置也先做参数检查


def test_network_missing_host_argparse_kills_shell_is_known_gap(capsys) -> None:
    """当前 argparse 缺参抛 SystemExit；交互 shell 无法回到提示符。

    钉死现状（P2，待 ChatGPT 修复后翻转）：single-shot 退出码 2，
    且交互 default 同样被 SystemExit 穿透。
    """
    shell = WindHubShell()
    with pytest.raises(SystemExit) as exc:
        shell.dispatch(["diagnose", "tcp"])
    assert exc.value.code == 2


def test_completion_includes_configured_identifiers(config_dir) -> None:
    shell = WindHubShell()
    shell.config.read(config_dir("modbus"))
    candidates = shell._candidates()
    assert "modbus-001" in candidates
    assert "active_power" in candidates
    assert "redis_latest" in candidates


def test_write_command_removed_from_persistent_history(config_dir, monkeypatch, tmp_path) -> None:
    import readline

    # 隔离 HOME，避免触碰真实 ~/.wind_hub_history。
    monkeypatch.setenv("HOME", str(tmp_path))
    shell = WindHubShell()
    shell.preloop()
    readline.clear_history()
    readline.add_history("diagnose protocol read modbus-001 active_power")
    readline.add_history("diagnose protocol write modbus-001 active_power 1 --yes")
    shell.precmd("diagnose protocol write modbus-001 active_power 1")
    length = readline.get_current_history_length()
    kept = [readline.get_history_item(i) for i in range(1, length + 1)]
    assert not any(" write " in f" {item} " for item in kept if item)
    assert any("read" in item for item in kept if item)
    shell.postloop()
