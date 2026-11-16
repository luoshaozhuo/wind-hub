"""Native Linux process lifetime, identity fencing, and cleanup tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

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
    state_dir.mkdir()
    (state_dir / "native-demo.json").write_text(
        json.dumps({"pid": os.getpid(), "start_tick": "invalid-stale-identity"}),
        encoding="utf-8",
    )
    outcome = await manage_native_process(target, "stop")
    assert outcome["already_stopped"]
    assert not (state_dir / "native-demo.json").exists()


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
