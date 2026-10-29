"""协议诊断 execute/listen/benchmark 与 shell protocol 命令契约。"""

from __future__ import annotations

import asyncio

import pytest

from cli.main import WindHubShell
from cli.protocol_diagnostic import execute
from cli.protocol_stream import benchmark, listen
from core.application.protocol_contract import (
    ProtocolCapability,
    WriteOutcome,
)
from core.domain.sample_value import Quality
from core.infrastructure.protocol.iec104.driver import (
    IEC104InterrogationIncompleteError,
)

MODBUS = "modbus-001"
IEC = "wtg-101"


def _loaded_shell(config_dir, name: str = "modbus") -> WindHubShell:
    shell = WindHubShell()
    shell.config.read(config_dir(name))
    return shell


# ---- execute 基础操作 ----


async def test_check_healthy(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    result = await execute(snapshot, MODBUS, "check")
    assert result == [{"healthy": True, "message": None}]
    driver = fake_drivers[MODBUS]
    assert driver.connects == 1 and driver.closes == 1


async def test_check_unhealthy_raises_and_closes(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    fake_drivers[MODBUS].healthy = False
    fake_drivers[MODBUS].health_message = "no link"
    with pytest.raises(RuntimeError, match="no link"):
        await execute(snapshot, MODBUS, "check")
    assert fake_drivers[MODBUS].closes == 1


async def test_read_single_uses_read_one(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    result = await execute(snapshot, MODBUS, "read", point_ids=["active_power"])
    assert len(result) == 1 and result[0]["point_id"] == "active_power"
    assert fake_drivers[MODBUS].read_one_calls == 1
    assert fake_drivers[MODBUS].read_many_calls == 0


async def test_read_many_native_batch(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    result = await execute(snapshot, MODBUS, "read-many", point_ids=["active_power", "wind_speed"])
    assert len(result) == 2
    assert fake_drivers[MODBUS].read_many_calls == 1
    assert fake_drivers[MODBUS].read_one_calls == 0


async def test_read_many_without_capability_raises(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    driver = fake_drivers[MODBUS]
    driver._capabilities = frozenset({ProtocolCapability.READ})
    with pytest.raises(ValueError, match="read_many"):
        await execute(snapshot, MODBUS, "read-many", point_ids=["active_power", "wind_speed"])


async def test_repeated_read_uses_one_session(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    result = await execute(
        snapshot,
        MODBUS,
        "read",
        point_ids=["active_power"],
        interval=0.01,
        count=3,
    )
    assert len(result) == 3
    assert fake_drivers[MODBUS].connects == 1
    assert fake_drivers[MODBUS].closes == 1


# ---- 写入与 UNKNOWN 不重试 ----


async def test_write_success(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    result = await execute(snapshot, MODBUS, "write", point_ids=["active_power"], value=12)
    assert result[0]["outcome"] == "confirmed_success"
    assert fake_drivers[MODBUS].write_calls == 1


async def test_write_timeout_returns_unknown_without_retry(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    driver = fake_drivers[MODBUS]
    driver.write_timeout = True

    async def bounded() -> object:
        return await asyncio.wait_for(
            execute(
                snapshot,
                MODBUS,
                "write",
                point_ids=["active_power"],
                value=1,
                timeout=0.2,
            ),
            timeout=2,
        )

    result = await bounded()
    assert isinstance(result, list)
    assert result[0]["outcome"] == "unknown"
    assert "Do not retry" in result[0]["message"]
    assert driver.write_calls == 1  # 绝不自动重试
    assert driver.closes == 1


async def test_write_confirmed_failure(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    driver = fake_drivers[MODBUS]
    driver.write_outcome = WriteOutcome.CONFIRMED_FAILURE
    result = await execute(snapshot, MODBUS, "write", point_ids=["active_power"], value=0)
    assert result[0]["outcome"] == "confirmed_failure"


async def test_write_rejects_unsupported_capability(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    fake_drivers[MODBUS]._capabilities = frozenset({ProtocolCapability.READ})
    with pytest.raises(ValueError, match="write"):
        await execute(snapshot, MODBUS, "write", point_ids=["active_power"], value=1)


# ---- GI 部分成功与超时 ----


async def test_gi_success(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir, "iec104").config.current.snapshot
    result = await execute(snapshot, IEC, "gi")
    assert len(result) == 2
    assert fake_drivers[IEC].interrogate_calls == 1


async def test_gi_partial_incomplete_is_unknown_with_samples(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir, "iec104").config.current.snapshot
    driver = fake_drivers[IEC]

    async def partial(request: object) -> object:
        raise IEC104InterrogationIncompleteError(1, tuple(driver.sample_points[:1]), "timeout")

    driver.interrogate = partial  # type: ignore[method-assign]
    result = await execute(snapshot, IEC, "gi", common_address=1)
    assert result[0]["status"] == "UNKNOWN"
    assert result[0]["common_address"] == 1
    assert len(result[0]["samples"]) == 1


async def test_gi_generic_timeout_is_failure_not_unknown(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir, "iec104").config.current.snapshot

    async def boom(request: object) -> object:
        raise TimeoutError("apdu timeout")

    fake_drivers[IEC].interrogate = boom  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        await execute(snapshot, IEC, "gi", timeout=1)


# ---- 取消与资源释放 ----


async def test_cancelled_read_closes_driver_and_releases_ads(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    driver = fake_drivers[MODBUS]
    driver.read_gate = asyncio.Event()
    task = asyncio.create_task(execute(snapshot, MODBUS, "read", point_ids=["active_power"]))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert driver.closes == 1
    assert fake_drivers["_releases"] == [False]


async def test_bad_quality_sample_is_preserved(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    fake_drivers[MODBUS].quality = Quality.BAD
    result = await execute(snapshot, MODBUS, "read", point_ids=["active_power"])
    assert result[0]["quality"] == "bad"


# ---- listen / benchmark ----


async def test_listen_subscribe_lifecycle_and_samples(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    result = await listen(snapshot, MODBUS, ["active_power"], duration=0.05, timeout=2)
    driver = fake_drivers[MODBUS]
    assert len(result) == 2
    assert driver.events[:3] == ["prepare_ads", "connect", "subscribe_create"]
    assert driver.events[-2:] == ["subscribe_stop", "close"]


async def test_listen_native_listen_branch(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    driver = fake_drivers[MODBUS]
    driver._capabilities = frozenset({ProtocolCapability.READ, ProtocolCapability.LISTEN})
    result = await listen(snapshot, MODBUS, [], duration=0.05, timeout=2)
    assert len(result) == 2
    assert "listen_create" in driver.events and "subscribe_create" not in driver.events


async def test_listen_without_push_capability_raises(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    fake_drivers[MODBUS]._capabilities = frozenset({ProtocolCapability.READ})
    with pytest.raises(ValueError, match="subscribe or listen"):
        await listen(snapshot, MODBUS, ["active_power"], duration=0.05)


async def test_listen_cancelled_releases_resources(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    task = asyncio.create_task(listen(snapshot, MODBUS, ["active_power"], timeout=2))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    driver = fake_drivers[MODBUS]
    assert driver.closes == 1
    assert "subscribe_stop" in driver.events


async def test_benchmark_stats_and_single_session(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    result = await benchmark(
        snapshot,
        MODBUS,
        ["active_power", "wind_speed"],
        samples=5,
        interval=0,
        timeout=2,
    )
    assert result["samples"] == 5
    assert result["min_ms"] <= result["mean_ms"] <= result["max_ms"]
    assert result["p95_ms"] <= result["max_ms"]
    driver = fake_drivers[MODBUS]
    assert driver.connects == 1 and driver.closes == 1
    assert driver.read_many_calls == 5


async def test_benchmark_validates_bounds(config_dir, fake_drivers) -> None:
    snapshot = _loaded_shell(config_dir).config.current.snapshot
    with pytest.raises(ValueError):
        await benchmark(snapshot, MODBUS, ["active_power"], samples=0, interval=0)
    with pytest.raises(ValueError):
        await benchmark(snapshot, MODBUS, ["active_power"], samples=1, interval=-1)


# ---- shell protocol 命令层 ----


def test_shell_check_json_envelope(config_dir, fake_drivers, capsys) -> None:
    shell = _loaded_shell(config_dir)
    shell._format = "json"
    payload = shell.dispatch("diagnose protocol check modbus-001".split())
    assert isinstance(payload, dict) and payload["status"] == "PASS"
    assert payload["endpoint"]["host"]
    assert "protocol_options" in payload


def test_shell_write_unknown_exit_code_three(config_dir, fake_drivers, run_cli) -> None:
    base = config_dir("modbus")
    fake_drivers[MODBUS].write_timeout = True
    rc, out, _ = run_cli(
        [
            "--config",
            str(base),
            "--format",
            "json",
            "diagnose",
            "protocol",
            "write",
            MODBUS,
            "active_power",
            "1",
            "--yes",
            "--timeout",
            "0.2",
        ]
    )
    assert rc == 3
    assert '"status": "UNKNOWN"' in out
    assert fake_drivers[MODBUS].write_calls == 1


def test_shell_write_requires_yes_non_interactive(config_dir, run_cli) -> None:
    base = config_dir("modbus")
    rc, _, err = run_cli(
        ["--config", str(base), "diagnose", "protocol", "write", MODBUS, "active_power", "1"]
    )
    assert rc == 2
    assert "--yes" in err


def test_shell_write_confirmed_failure_exit_one(config_dir, fake_drivers, run_cli) -> None:
    base = config_dir("modbus")
    fake_drivers[MODBUS].write_outcome = WriteOutcome.CONFIRMED_FAILURE
    rc, out, _ = run_cli(
        [
            "--config",
            str(base),
            "--format",
            "json",
            "diagnose",
            "protocol",
            "write",
            MODBUS,
            "active_power",
            "1",
            "--yes",
        ]
    )
    assert rc == 1
    assert '"status": "FAIL"' in out


def test_shell_bad_quality_is_warning(config_dir, fake_drivers) -> None:
    shell = _loaded_shell(config_dir)
    fake_drivers[MODBUS].quality = Quality.BAD
    payload = shell.dispatch("diagnose protocol read modbus-001 active_power".split())
    assert isinstance(payload, dict) and payload["status"] == "WARNING"


def test_shell_gi_partial_is_unknown(config_dir, fake_drivers) -> None:
    shell = _loaded_shell(config_dir, "iec104")
    driver = fake_drivers[IEC]

    async def partial(request: object) -> object:
        raise IEC104InterrogationIncompleteError(1, tuple(driver.sample_points[:1]), "x")

    driver.interrogate = partial  # type: ignore[method-assign]
    payload = shell.dispatch(["diagnose", "protocol", "gi", IEC, "--ca", "1"])
    assert isinstance(payload, dict) and payload["status"] == "UNKNOWN"


def test_shell_continuous_json_requires_count(config_dir, run_cli) -> None:
    base = config_dir("modbus")
    rc, out, _ = run_cli(
        [
            "--config",
            str(base),
            "--format",
            "json",
            "diagnose",
            "protocol",
            "read",
            MODBUS,
            "active_power",
            "--interval",
            "1",
        ]
    )
    assert rc == 2
    # JSON 模式错误也走信封输出到 stdout。
    assert "--count" in out
    assert '"status": "FAIL"' in out


def test_shell_write_rejects_nan_and_objects(config_dir, run_cli) -> None:
    base = config_dir("modbus")
    rc, _, _ = run_cli(
        [
            "--config",
            str(base),
            "diagnose",
            "protocol",
            "write",
            MODBUS,
            "active_power",
            "NaN",
            "--yes",
        ]
    )
    assert rc == 2
    rc, _, _ = run_cli(
        [
            "--config",
            str(base),
            "diagnose",
            "protocol",
            "write",
            MODBUS,
            "active_power",
            '{"x": 1}',
            "--yes",
        ]
    )
    assert rc == 2


def test_shell_protocol_failure_envelope_keeps_endpoint(config_dir, fake_drivers) -> None:
    shell = _loaded_shell(config_dir)

    async def refuse_connect() -> object:
        raise ConnectionError("link down")

    fake_drivers[MODBUS].connect = refuse_connect  # type: ignore[method-assign]
    payload = shell.dispatch("diagnose protocol check modbus-001".split())
    assert isinstance(payload, dict)
    assert payload["status"] == "FAIL"
    assert payload["code"] == "PROTOCOL_DIAGNOSTIC_FAILED"
    assert payload["endpoint"]["host"]
    assert "ConnectionError" in payload["message"]
