"""诊断 Typer CLI 的真实命令调用测试（CliRunner，不 mock Typer 本身）。

- 子命令注册与 --help；
- PASS/FAIL/UNKNOWN/SKIPPED/WARNING → 退出码 0/1/3（WARNING 与 PASS 同为 0）；
- --json 输出可解析、可承载特殊字符；
- 参数边界经 Typer BadParameter 退出码 2。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from core.application.diagnostic import DiagnosticFinding, DiagnosticResult, DiagnosticStatus
from core.infrastructure.diagnostic import cli, firewall, network, sink

runner = CliRunner()
FIXTURE = Path("tests/fixtures/configs/example_modbus")


def _result(status: DiagnosticStatus, *, code: str = "X", message: str = "msg") -> DiagnosticResult:
    return DiagnosticResult(
        category="test",
        target="target",
        status=status,
        findings=(DiagnosticFinding(code=code, status=status, message=message),),
        measurements={"k": 1},
        evidence={"e": True},
    )


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["config", "--help"],
        ["network", "--help"],
        ["network", "firewall", "--help"],
        ["protocol", "--help"],
        ["sink", "--help"],
    ],
)
def test_help_and_subcommand_registration(args: list[str]) -> None:
    result = runner.invoke(cli.app, args)
    # 无参数时 Typer no_args_is_help 以退出码 2 打印帮助。
    assert result.exit_code == (2 if not args else 0)
    assert "Usage" in result.output


def test_config_check_valid_fixture_exit_zero_json() -> None:
    result = runner.invoke(cli.app, ["config", "check", str(FIXTURE), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout if hasattr(result, "stdout") else result.output)
    assert payload["status"] == "PASS"
    assert payload["measurements"]["devices"] >= 1


def test_config_check_missing_directory_exit_1() -> None:
    result = runner.invoke(cli.app, ["config", "check", "/no/such/dir"])

    assert result.exit_code == 1


def test_config_diff_invalid_side_exit_1(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["config", "diff", str(tmp_path / "a"), str(tmp_path / "b")])
    assert result.exit_code == 1


def test_network_tcp_closed_port_exit_1() -> None:
    result = runner.invoke(cli.app, ["network", "tcp", "127.0.0.1", "1", "--timeout", "0.3"])
    assert result.exit_code == 1
    assert "TCP_CONNECT_FAILED" in result.output


def test_network_tcp_open_port_exit_0() -> None:
    # CLI 内部自行 asyncio.run，不能在已有事件循环里调用；用线程态 TCP 服务器。
    import socketserver
    import threading

    class _Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = runner.invoke(
                cli.app, ["network", "tcp", "127.0.0.1", str(server.server_address[1])]
            )
            assert result.exit_code == 0, result.output
        finally:
            server.shutdown()
            thread.join()


def test_network_scan_invalid_ports_exit_2() -> None:
    result = runner.invoke(
        cli.app,
        ["network", "scan", "127.0.0.1", "--ports", "0,70000", "--timeout", "0.1"],
    )
    assert result.exit_code == 2


def test_network_scan_empty_ports_exit_2() -> None:
    result = runner.invoke(
        cli.app, ["network", "scan", "127.0.0.1", "--ports", " , ", "--timeout", "0.1"]
    )
    assert result.exit_code == 2


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (DiagnosticStatus.FAIL, 1),
        (DiagnosticStatus.UNKNOWN, 3),
        (DiagnosticStatus.SKIPPED, 3),
        (DiagnosticStatus.WARNING, 0),
        (DiagnosticStatus.PASS, 0),
    ],
)
def test_exit_codes_by_status(
    monkeypatch: pytest.MonkeyPatch, status: DiagnosticStatus, code: int
) -> None:
    async def fake_check(*args, **kwargs) -> DiagnosticResult:
        return _result(status)

    monkeypatch.setattr(sink, "check_sink", fake_check)
    result = runner.invoke(cli.app, ["sink", "check", "--sink", "s1", "--config", "x"])
    assert result.exit_code == code


def test_firewall_check_exit_3_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_firewall_check(port: int, *, direction: str, protocol: str) -> DiagnosticResult:
        return _result(DiagnosticStatus.UNKNOWN, code="FIREWALL_DECISION_UNDETERMINED")

    monkeypatch.setattr(firewall, "firewall_check", fake_firewall_check)
    result = runner.invoke(
        cli.app,
        [
            "network",
            "firewall",
            "check",
            "--port",
            "502",
            "--direction",
            "inbound",
            "--protocol",
            "tcp",
        ],
    )
    assert result.exit_code == 3


def test_firewall_check_bad_direction_exit_2() -> None:
    result = runner.invoke(
        cli.app,
        ["network", "firewall", "check", "--port", "502", "--direction", "sideways"],
    )
    assert result.exit_code == 2


def test_json_output_serializes_special_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = '中文与 emoji 🔥 以及引号 "q"'

    async def fake_check(*args, **kwargs) -> DiagnosticResult:
        return _result(DiagnosticStatus.PASS, message=message)

    monkeypatch.setattr(sink, "check_sink", fake_check)
    result = runner.invoke(cli.app, ["sink", "check", "--sink", "s1", "--config", "x", "--json"])

    assert result.exit_code == 0
    text = result.stdout if hasattr(result, "stdout") else result.output
    payload = json.loads(text)
    assert payload["findings"][0]["message"] == message
    assert "🔥" in text  # ensure_ascii=False，不转义成 \uXXXX


def test_network_ping_invalid_count_exit_2() -> None:
    result = runner.invoke(cli.app, ["network", "ping", "h", "--count", "0"])
    assert result.exit_code == 2


def test_protocol_benchmark_duplicate_points_exit_2() -> None:
    result = runner.invoke(
        cli.app,
        [
            "protocol",
            "benchmark",
            "--device",
            "d",
            "--config",
            "x",
            "--points",
            "p,p",
        ],
    )
    assert result.exit_code == 2


def test_plain_output_includes_findings_and_measurements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_interfaces() -> DiagnosticResult:
        return _result(DiagnosticStatus.PASS, code="IP_QUERY_OK")

    monkeypatch.setattr(network, "interfaces", fake_interfaces)
    result = runner.invoke(cli.app, ["network", "interfaces"])

    assert result.exit_code == 0
    assert "test: target: PASS" in result.output
    assert "IP_QUERY_OK" in result.output
    assert "k: 1" in result.output


# --------------------------------------------------------------------------
# 命令注册接线：所有子命令都能真实调到对应实现（实现本身以桩替换）
# --------------------------------------------------------------------------


def _ok_result() -> DiagnosticResult:
    return _result(DiagnosticStatus.PASS, code="WIRED")


def test_network_routes_command_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_routes() -> DiagnosticResult:
        return _ok_result()

    monkeypatch.setattr(network, "routes", fake_routes)
    assert runner.invoke(cli.app, ["network", "routes"]).exit_code == 0


def test_network_ports_command_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ports(**kwargs) -> DiagnosticResult:
        assert kwargs == {"listening": True, "established": False, "port": None}
        return _ok_result()

    monkeypatch.setattr(network, "ports", fake_ports)
    result = runner.invoke(cli.app, ["network", "ports", "--listening"])
    assert result.exit_code == 0


def test_network_port_subcommand_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def fake_ports(*, listening: bool = False, established: bool = False, port=None):
        captured.update(listening=listening, established=established, port=port)
        return _ok_result()

    monkeypatch.setattr(network, "ports", fake_ports)
    result = runner.invoke(cli.app, ["network", "port", "502"])
    assert result.exit_code == 0
    assert captured == {"listening": False, "established": False, "port": 502}


def test_network_ping_command_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def fake_ping(host, *, count, timeout) -> DiagnosticResult:
        captured.update(host=host, count=count, timeout=timeout)
        return _ok_result()

    monkeypatch.setattr(network, "ping_probe", fake_ping)
    result = runner.invoke(
        cli.app, ["network", "ping", "10.0.0.1", "--count", "2", "--timeout", "0.5"]
    )
    assert result.exit_code == 0
    assert captured == {"host": "10.0.0.1", "count": 2, "timeout": 0.5}


def test_network_scan_command_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def fake_scan(host, ports, *, timeout, concurrency) -> DiagnosticResult:
        captured.update(ports=ports, concurrency=concurrency)
        return _ok_result()

    monkeypatch.setattr(network, "scan_ports", fake_scan)
    result = runner.invoke(
        cli.app,
        [
            "network",
            "scan",
            "10.0.0.1",
            "--ports",
            "502, 4840",
            "--timeout",
            "0.5",
            "--concurrency",
            "2",
        ],
    )
    assert result.exit_code == 0
    assert captured["ports"] == [502, 4840]
    assert captured["concurrency"] == 2


def test_network_bandwidth_command_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def fake_bandwidth(host, *, duration, port) -> DiagnosticResult:
        captured.update(duration=duration, port=port)
        return _ok_result()

    monkeypatch.setattr(network, "bandwidth", fake_bandwidth)
    result = runner.invoke(
        cli.app,
        ["network", "bandwidth", "10.0.0.1", "--duration", "3", "--port", "5201"],
    )
    assert result.exit_code == 0
    assert captured == {"duration": 3, "port": 5201}


def test_firewall_status_and_rules_commands_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_status() -> DiagnosticResult:
        return _ok_result()

    monkeypatch.setattr(firewall, "firewall_status", fake_status)
    assert runner.invoke(cli.app, ["network", "firewall", "status"]).exit_code == 0
    assert runner.invoke(cli.app, ["network", "firewall", "rules"]).exit_code == 0


def test_protocol_check_and_point_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def fake_check(config, device_id, *, point_id=None, timeout):
        captured.update(config=str(config), device=device_id, point=point_id)
        return _ok_result()

    monkeypatch.setattr(cli.protocol, "check_protocol", fake_check)
    result = runner.invoke(
        cli.app,
        ["protocol", "check", "--device", "dev-1", "--config", str(FIXTURE)],
    )
    assert result.exit_code == 0
    assert captured["device"] == "dev-1"
    assert captured["point"] is None

    result = runner.invoke(
        cli.app,
        [
            "protocol",
            "point",
            "--device",
            "dev-1",
            "--point",
            "freq",
            "--config",
            str(FIXTURE),
        ],
    )
    assert result.exit_code == 0
    assert captured["point"] == "freq"


def test_protocol_benchmark_command_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def fake_benchmark(config, device_id, point_ids, *, samples, interval, timeout):
        captured.update(ids=point_ids, samples=samples, interval=interval)
        return _ok_result()

    monkeypatch.setattr(cli.protocol, "benchmark_protocol", fake_benchmark)
    result = runner.invoke(
        cli.app,
        [
            "protocol",
            "benchmark",
            "--device",
            "dev-1",
            "--config",
            "x",
            "--points",
            "p1,p2",
            "--samples",
            "3",
        ],
    )
    assert result.exit_code == 0
    assert captured["ids"] == ["p1", "p2"]
    assert captured["samples"] == 3


def test_sink_benchmark_command_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_benchmark(config, sink_id, *, samples, interval, timeout) -> DiagnosticResult:
        assert sink_id == "s1"
        return _ok_result()

    monkeypatch.setattr(sink, "benchmark_sink", fake_benchmark)
    result = runner.invoke(
        cli.app,
        ["sink", "benchmark", "--sink", "s1", "--config", "x", "--samples", "2"],
    )
    assert result.exit_code == 0


def test_config_diff_unchanged_wired() -> None:
    result = runner.invoke(cli.app, ["config", "diff", str(FIXTURE), str(FIXTURE), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout if hasattr(result, "stdout") else result.output)
    assert payload["evidence"]["has_changes"] is False
