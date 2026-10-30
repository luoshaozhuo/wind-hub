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


def test_help_text_matches_actual_parser_contract() -> None:
    """Help 声明的语法必须与 dispatch 实际接受的语法一致（P2-3）。"""
    validate_help = help_text(["config", "validate"])
    assert "--scope all" in validate_help and "--scope=all" in validate_help
    gateways_help = help_text(["gateways"])
    assert "[GATEWAY_ID]" in gateways_help
    assert "At most one Gateway ID" in gateways_help
    diagnose_help = help_text(["diagnose"])
    assert "diagnose ping/tcp/route/resolve" in diagnose_help
    # 层级 help 均可渲染，最长前缀回退到具体子命令。
    for path in (
        ["config"],
        ["config", "validate"],
        ["gateways"],
        ["diagnose"],
        ["diagnose", "protocol", "read"],
        ["diagnose", "protocol", "write"],
    ):
        assert help_text(path).strip()


def test_legacy_cli_client_module_removed() -> None:
    """旧 cli.client 模块已删除：不得再被 import（P2-4 回归钉死）。"""
    import importlib.util

    assert importlib.util.find_spec("cli.client") is None


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


def test_network_argument_errors_raise_value_error_not_systemexit() -> None:
    """网络子命令缺参/非法参数必须走 ValueError，绝不 SystemExit 穿透交互 shell。"""
    shell = WindHubShell()
    # 缺少必填 host（四个网络子命令同源）。
    for operation in ("tcp", "ping", "resolve", "route"):
        with pytest.raises(ValueError, match="arguments are required: host"):
            shell.dispatch(["diagnose", operation])
    # 无法识别的参数与非法端口同样是 ValueError 而非 SystemExit
    # （argparse 可能先报缺 host，消息文本不锁死，只锁异常类型）。
    with pytest.raises(ValueError, match="diagnose tcp"):
        shell.dispatch(["diagnose", "tcp", "--unknown"])
    with pytest.raises(ValueError, match="invalid int value"):
        shell.dispatch(["diagnose", "tcp", "127.0.0.1", "not-a-port"])


def test_interactive_shell_survives_network_argument_error(capsys) -> None:
    """交互模式下参数错误只打印 FAIL；下一条有效命令必须照常执行。"""
    shell = WindHubShell()
    shell.default("diagnose tcp")  # 缺 host：不得抛出，不得退出进程
    captured = capsys.readouterr()
    assert "FAIL" in captured.out
    assert "CLI_ERROR" in captured.out
    # 错误后 help 仍可用，证明 shell 存活在提示符上。
    shell.default("help diagnose")
    help_out = capsys.readouterr().out
    assert "diagnose protocol OP DEVICE" in help_out


def test_network_argument_error_single_shot_exit_code(run_cli) -> None:
    """单次命令模式保持 argparse 传统退出码 2。"""
    rc, _, err = run_cli(["diagnose", "tcp"])
    assert rc == 2
    assert "FAIL" in err  # table 模式错误走 stderr
    rc, out, _ = run_cli(["--format", "json", "diagnose", "tcp"])
    assert rc == 2
    assert "CLI_ERROR" in out  # json 模式错误是结构化结果
    rc, _, _ = run_cli(["diagnose", "ping", "--bogus"])
    assert rc == 2


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
