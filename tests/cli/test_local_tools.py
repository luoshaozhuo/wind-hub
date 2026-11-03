"""本地纯逻辑验收：网络探针（真实本机 socket）、目录 diff、快照导出、历史脱敏、确认门。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from cli.directory_diff import compare_directories
from cli.history import SafeHistory
from cli.network_probe import MAX_CONCURRENCY, MAX_HOSTS, industrial_probe, scan_cidr, tcp_probe
from cli.safety import confirm_mutation
from cli.snapshot_io import export_snapshot


async def _echo_server() -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1] if server.sockets else 0
    return server, port


async def test_tcp_probe_open_closed_timeout() -> None:
    server, port = await _echo_server()
    try:
        opened = await tcp_probe("127.0.0.1", port, timeout=1)
        assert opened["tcp_state"] == "OPEN"
        assert opened["latency_ms"] >= 0
    finally:
        server.close()
        await server.wait_closed()
    closed = await tcp_probe("127.0.0.1", port, timeout=1)
    assert closed["tcp_state"] == "CLOSED"


async def test_tcp_probe_input_validation() -> None:
    with pytest.raises(ValueError):
        await tcp_probe("not-an-ip", 1)
    with pytest.raises(ValueError):
        await tcp_probe("127.0.0.1", 0)
    with pytest.raises(ValueError):
        await tcp_probe("127.0.0.1", 1, timeout=99)


async def test_industrial_probe_uses_preset_ports() -> None:
    results = await industrial_probe("127.0.0.1", timeout=0.2, ports=(1, 2, 3))
    assert {item["port"] for item in results} == {1, 2, 3}
    assert all(item["tcp_state"] == "CLOSED" for item in results)
    with pytest.raises(ValueError):
        await industrial_probe("127.0.0.1", ports=())


async def test_scan_cidr_rejects_oversized_network() -> None:
    with pytest.raises(ValueError, match="exceeds configured limit"):
        await scan_cidr("10.0.0.0/8", max_hosts=16)


async def test_scan_cidr_enforces_hard_concurrency_bounds() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        await scan_cidr("127.0.0.0/30", concurrency=MAX_CONCURRENCY + 1)
    with pytest.raises(ValueError, match="max_hosts"):
        await scan_cidr("127.0.0.0/30", max_hosts=MAX_HOSTS + 1)


async def test_scan_cidr_real_local_subnet_bounded() -> None:
    outcome = await scan_cidr("127.0.0.0/31", timeout=1, concurrency=2, max_hosts=4)
    assert outcome["network"] == "127.0.0.0/31"
    assert outcome["scanned"] == len(outcome["results"])
    assert {item["status"] for item in outcome["results"]} <= {"UP", "NO_RESPONSE"}


def test_ping_executable_present_or_skip() -> None:
    if shutil.which("ping") is None:
        pytest.skip("本机无 ping 可执行文件")
    # 只验证 ping 子进程在当前非特权环境下的真实返回约定；WSL/容器可能禁止 ICMP。
    completed = subprocess.run(
        ["ping", "-c", "1", "-W", "1", "127.0.0.1"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode in (0, 1, 2)


def _make_tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_directory_diff_added_removed_modified(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _make_tree(left, {"same.txt": "a\n", "old.txt": "x\n", "change.txt": "1\n"})
    _make_tree(right, {"same.txt": "a\n", "new.txt": "y\n", "change.txt": "2\n"})
    result = compare_directories(left, right)
    assert result["added"] == ["new.txt"]
    assert result["removed"] == ["old.txt"]
    assert result["modified"] == ["change.txt"]
    assert result["unchanged"] == 1
    assert "@@" in result["text_diff"]["change.txt"]


def test_directory_diff_rejects_symlink(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "real.txt").write_text("a", encoding="utf-8")
    (right / "link.txt").symlink_to(left / "real.txt")
    with pytest.raises(ValueError, match="symbolic links"):
        compare_directories(left, right)


def test_directory_diff_redacts_sensitive_lines_and_key_files(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _make_tree(
        left,
        {"svc.yaml": "password: secret-a\nport: 1\n", "client.key": "old-pem\n"},
    )
    _make_tree(
        right,
        {"svc.yaml": "password: secret-b\nport: 2\n", "client.key": "new-pem\n"},
    )
    result = compare_directories(left, right)
    diff = result["text_diff"]["svc.yaml"]
    assert "secret-a" not in diff and "secret-b" not in diff
    assert "password: [REDACTED]" in diff
    assert "port: 2" in diff  # 非敏感差异仍保留
    assert result["text_diff"]["client.key"] == "[SENSITIVE FILE REDACTED]"


def test_directory_diff_truncation_is_display_only(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    _make_tree(left, {"f": "\n".join(f"old-{i}" for i in range(20))})
    _make_tree(right, {"f": "\n".join(f"new-{i}" for i in range(20))})
    result = compare_directories(left, right, max_output_lines=3)
    assert result["text_diff_truncated"] is True
    assert result["modified"] == ["f"]  # 修改判定不受截断影响


def _snapshot_file(name: str, content: bytes) -> dict[str, object]:
    return {
        "relative_path": name,
        "content": base64.b64encode(content).decode(),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def test_export_snapshot_happy_path_atomic(tmp_path: Path) -> None:
    payload = _snapshot_file("conf/a.yaml", b"services: []\n")
    destination = tmp_path / "out"
    summary = export_snapshot(
        {"snapshot_id": "s1", "revision": "r1", "files": [payload]}, destination, 1024
    )
    assert summary["file_count"] == 1
    assert (destination / "conf" / "a.yaml").read_bytes() == b"services: []\n"
    assert (destination / "conf" / "a.yaml").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "name",
    ["../escape.yaml", "/abs.yaml", "a/./b", "a/../b.yaml", "a\\b.yaml"],
)
def test_export_snapshot_blocks_traversal(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="traversal|path"):
        export_snapshot(
            {"files": [_snapshot_file(name, b"x")]},
            tmp_path / "out",
            1024,
        )


def test_export_snapshot_rejects_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(ValueError, match="must not already exist"):
        export_snapshot({"files": []}, destination, 1024)


def test_export_snapshot_checksum_and_size_mismatch(tmp_path: Path) -> None:
    payload = _snapshot_file("a", b"abc")
    payload["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        export_snapshot({"files": [payload]}, tmp_path / "out1", 1024)
    payload2 = _snapshot_file("a", b"abc")
    payload2["size_bytes"] = 99
    with pytest.raises(ValueError, match="size"):
        export_snapshot({"files": [payload2]}, tmp_path / "out2", 1024)


def test_export_snapshot_rejects_duplicates_and_bad_base64(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        export_snapshot(
            {"files": [_snapshot_file("a", b"x"), _snapshot_file("a", b"y")]},
            tmp_path / "out",
            1024,
        )
    payload = _snapshot_file("a", b"x")
    payload["content"] = "not base64!!"
    with pytest.raises(ValueError):
        export_snapshot({"files": [payload]}, tmp_path / "out2", 1024)


def test_safe_history_suppresses_mutations_and_secrets() -> None:
    history = SafeHistory()
    history.store_string("collector devices")
    history.store_string("commander write dev p '1' --yes token=abc")
    history.store_string("config push ./candidate")
    history.store_string("config apply c1")
    history.store_string('commander result "secret-value"')
    assert history._storage == ["collector devices"]


def test_confirm_mutation_fail_closed_without_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    import typer

    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    with pytest.raises(typer.Exit) as exc_info:
        confirm_mutation("do it", False, command="test")
    assert exc_info.value.exit_code == 2
    confirm_mutation("do it", True)  # --yes 直通
