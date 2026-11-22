"""Native Linux process lifetime, identity fencing, and cleanup tests."""

from __future__ import annotations

import json
import os
import socket
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from cli import native_process
from cli.native_process import manage_native_process
from cli.runtime import ProcessSpec, ServiceTarget

pytestmark = [pytest.mark.system, pytest.mark.real_service]


def _target(root: Path, *code: str, timeout: float = 5.0) -> ServiceTarget:
    return ServiceTarget(
        service_id="native-demo",
        grpc_endpoint=None,
        tls_profile=None,
        manager="process",
        target="native-demo",
        namespace=None,
        manager_socket=None,
        isula_binary=None,
        process=ProcessSpec(
            argv=(sys.executable, "-c", ";".join(code)),
            cwd=root,
            env={},
            state_dir=root / "state",
            log_file=root / "service.log",
        ),
        timeout=timeout,
    )


async def test_native_lifecycle_and_idempotence(tmp_path: Path) -> None:
    target = _target(tmp_path, "import time", "time.sleep(120)")
    assert (await manage_native_process(target, "status"))["state"] == "STOPPED"
    try:
        started = await manage_native_process(target, "start")
        assert started["confirmed"] and started["running"]
        again = await manage_native_process(target, "start")
        assert again["already_running"] and again["pid"] == started["pid"]
        restarted = await manage_native_process(target, "restart")
        assert restarted["confirmed"] and restarted["pid"] != started["pid"]
    finally:
        await manage_native_process(target, "stop")
    assert (await manage_native_process(target, "status"))["state"] == "STOPPED"
    assert (await manage_native_process(target, "stop"))["already_stopped"]


async def test_stale_pid_identity_never_signals_unowned_pid(tmp_path: Path) -> None:
    target = _target(tmp_path, "import time", "time.sleep(120)")
    state_dir = target.process.state_dir if target.process else tmp_path
    state_dir.mkdir(mode=0o700)
    (state_dir / "native-demo.json").write_text(
        json.dumps({"pid": os.getpid(), "start_tick": "invalid-stale-identity"}),
        encoding="utf-8",
    )
    outcome = await manage_native_process(target, "stop")
    assert outcome["already_stopped"]
    assert not (state_dir / "native-demo.json").exists()


async def test_occupied_loopback_endpoint_cannot_confirm_foreign_service(
    tmp_path: Path,
) -> None:
    """A live unrelated listener must not make a failed spawn look ready."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        endpoint = f"127.0.0.1:{listener.getsockname()[1]}"
        target = replace(
            _target(tmp_path, "import time", "time.sleep(120)"),
            grpc_endpoint=endpoint,
        )
        with pytest.raises(RuntimeError, match="already occupied"):
            await manage_native_process(target, "start")
        assert (await manage_native_process(target, "status"))["state"] == "STOPPED"


async def test_early_child_failure_is_not_confirmed_as_service_start(
    tmp_path: Path,
) -> None:
    target = replace(
        _target(tmp_path, "import sys", "sys.exit(19)"),
        grpc_endpoint="127.0.0.1:1",
    )
    with pytest.raises(RuntimeError, match="exited during start confirmation"):
        await manage_native_process(target, "start")
    assert (await manage_native_process(target, "status"))["state"] == "STOPPED"


async def test_restart_waits_for_previous_listener_to_disappear(tmp_path: Path) -> None:
    target = replace(
        _target(tmp_path, "import time", "time.sleep(120)"),
        grpc_endpoint="127.0.0.1:1",
    )
    started = await manage_native_process(target, "start")
    try:
        with patch.object(native_process, "_foreign_loopback_listener", side_effect=[True, False]):
            restarted = await manage_native_process(target, "restart")
        assert restarted["confirmed"] and restarted["pid"] != started["pid"]
        assert [stage["operation"] for stage in restarted["stages"]] == ["stop", "start"]
    finally:
        await manage_native_process(target, "stop")


async def test_foreign_health_endpoint_not_owned_by_managed_process(
    tmp_path: Path,
) -> None:
    from cli.native_process import managed_endpoint_owned

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        target = replace(
            _target(tmp_path, "import time", "time.sleep(120)"),
            grpc_endpoint=f"127.0.0.1:{port}",
        )
        # The manager's process may exist, but it cannot own a socket opened
        # by the foreign test process.
        with patch("cli.native_process._foreign_loopback_listener", return_value=False):
            await manage_native_process(target, "start")
        try:
            assert not managed_endpoint_owned(target)
        finally:
            await manage_native_process(target, "stop")


@pytest.mark.parametrize(
    ("family", "listener_host", "endpoint_host"),
    [
        (socket.AF_INET, "127.0.0.1", "127.0.0.1"),
        (socket.AF_INET6, "::1", "[::1]"),
        (socket.AF_INET6, "::ffff:127.0.0.1", "127.0.0.1"),
    ],
)
async def test_managed_listener_ownership_matches_process_group(
    tmp_path: Path,
    family: socket.AddressFamily,
    listener_host: str,
    endpoint_host: str,
) -> None:
    import asyncio

    from cli.native_process import managed_endpoint_owned

    with socket.socket(family, socket.SOCK_STREAM) as reservation:
        reservation.bind((listener_host, 0))
        port = reservation.getsockname()[1]
    target = replace(
        _target(
            tmp_path,
            "import socket, time",
            f"listener = socket.socket(socket.{family.name})",
            f"listener.bind(({listener_host!r}, {port}))",
            "listener.listen()",
            "time.sleep(120)",
        ),
        grpc_endpoint=f"{endpoint_host}:{port}",
    )
    try:
        started = await manage_native_process(target, "start")
        for _ in range(30):
            if managed_endpoint_owned(target):
                break
            await asyncio.sleep(0.1)
        assert managed_endpoint_owned(
            target,
            expected_pid=started["pid"],
            expected_start_tick=started["start_tick"],
        )
        assert not managed_endpoint_owned(
            target,
            expected_pid=started["pid"],
            expected_start_tick="invalid-generation",
        )
    finally:
        await manage_native_process(target, "stop")
    assert not managed_endpoint_owned(target)


async def test_cli_rejects_healthy_foreign_endpoint_after_process_start(
    tmp_path: Path,
) -> None:
    from cli import main

    target = replace(
        _target(tmp_path, "import time", "time.sleep(120)", timeout=0.0),
        grpc_endpoint="127.0.0.1:45321",
    )

    class FakeRuntime:
        def target(self, _name: str) -> ServiceTarget:
            return target

        async def call_common(self, _name: str, _method: str) -> dict[str, str]:
            return {"health": "HEALTHY"}

    async def fake_start(_target: ServiceTarget, _action: str) -> dict[str, object]:
        return {"confirmed": True, "running": True, "pid": 12345, "start_tick": "42"}

    with (
        patch("cli.main.runtime", return_value=FakeRuntime()),
        patch("cli.main.manage_service_process", side_effect=fake_start),
        patch("cli.native_process.managed_endpoint_owned", side_effect=[True, False]) as ownership,
        pytest.raises(RuntimeError, match="does not belong to managed process"),
    ):
        await main._manage_service("configuration", "start")
    assert ownership.call_count == 2


async def test_native_process_exit_is_not_reported_running(tmp_path: Path) -> None:
    target = _target(tmp_path, "import time", "time.sleep(0.3)")
    try:
        started = await manage_native_process(target, "start")
        assert started["running"]
        import asyncio

        await asyncio.sleep(0.6)
        result = await manage_native_process(target, "status")
        assert result["state"] == "STOPPED"
    finally:
        await manage_native_process(target, "stop")


async def test_process_group_workers_are_reaped(tmp_path: Path) -> None:
    target = _target(
        tmp_path,
        "import subprocess, time",
        "subprocess.Popen(['sleep', '120'])",
        "time.sleep(120)",
    )
    try:
        started = await manage_native_process(target, "start")
        assert started["running"]
        stopped = await manage_native_process(target, "stop")
        assert stopped["confirmed"] and stopped["state"] == "STOPPED"
    finally:
        await manage_native_process(target, "stop")


async def test_concurrent_starts_create_one_process(tmp_path: Path) -> None:
    target = _target(tmp_path, "import time", "time.sleep(120)")
    try:
        import asyncio

        first, second = await asyncio.gather(
            manage_native_process(target, "start"), manage_native_process(target, "start")
        )
        assert first["pid"] == second["pid"]
        assert first["running"] and second["running"]
        assert first.get("already_running") or second.get("already_running")
    finally:
        await manage_native_process(target, "stop")


async def test_state_save_failure_reaps_started_group(tmp_path: Path) -> None:
    target = _target(tmp_path, "import time", "time.sleep(120)")
    with (
        patch("cli.native_process._save_state", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        await manage_native_process(target, "start")
    assert (await manage_native_process(target, "status"))["state"] == "STOPPED"


def test_orphaned_group_guard_detects_active_pgid(tmp_path: Path) -> None:
    pid = os.getpid()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.chmod(0o700)
    state_file = state_dir / "native-demo.json"
    state_file.write_text(json.dumps({"pid": pid, "start_tick": "invalid"}), encoding="utf-8")
    target = _target(tmp_path)
    assert target.process is not None
    target = replace(target, process=replace(target.process, state_dir=state_dir))

    with (
        patch("cli.native_process._group_active", return_value=True),
        pytest.raises(RuntimeError, match="orphaned or conflicting worker group"),
    ):
        native_process._manage(target, "start")
    assert json.loads(state_file.read_text(encoding="utf-8"))["pid"] == pid
