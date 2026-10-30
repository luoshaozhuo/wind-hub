"""自动诊断流水线：顺序、单会话复用、失败即停、断点恢复、汇总状态。"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from cli.config_session import ConfigSession
from cli.main import WindHubShell
from cli.pipeline import _resume, _sink_address, plan, run


def _snapshot(config_dir, name: str = "ads"):
    loaded = ConfigSession().read(config_dir(name))
    return loaded.snapshot, loaded.fingerprint


def test_plan_order_and_determinism(config_dir) -> None:
    snapshot, _ = _snapshot(config_dir, "ads")
    steps = plan(snapshot)
    kinds = [(step["kind"], step["target"]) for step in steps]
    # 两台设备：每台先 check 后逐点 read；顺序按设备 ID 稳定。
    assert kinds[0] == ("protocol_check", "ads-001")
    assert kinds[1] == ("point_read", "ads-001")
    assert kinds[-4] == ("protocol_check", "ads-010")
    targets = [step["target"] for step in steps]
    assert targets.index("ads-010") > targets.index("ads-001")
    assert plan(snapshot) == steps


def test_plan_disabled_devices_and_sink_filter(config_dir) -> None:
    snapshot, _ = _snapshot(config_dir, "modbus")
    steps = plan(snapshot)
    assert any(step["kind"] == "sink_endpoint" for step in steps)
    # 只有 read/read_write 点进入计划。
    assert all(step["kind"] != "point_read" or step["point"] for step in steps)


async def test_run_one_driver_session_per_device(config_dir, fake_drivers) -> None:
    snapshot, fingerprint = _snapshot(config_dir, "ads")
    result = await run(snapshot, fingerprint, timeout=5)
    assert result["status"] == "PASS"
    assert result["failed_index"] is None
    assert result["completed"] == result["total"]
    first = fake_drivers["ads-001"]
    second = fake_drivers["ads-010"]
    # 每台设备仅 connect 一次：check 与 3 个点读复用同一会话。
    assert first.connects == 1 and first.closes == 1
    assert second.connects == 1 and second.closes == 1
    assert first.read_one_calls == 3 and second.read_one_calls == 3
    # 切换设备时先关旧会话、再连新会话（全局事件序）。
    order = fake_drivers["_order"]
    assert order.index(("ads-001", "close")) < order.index(("ads-010", "connect"))


async def test_run_fail_fast_closes_sessions(config_dir, fake_drivers) -> None:
    snapshot, fingerprint = _snapshot(config_dir, "ads")
    fake_drivers["ads-010"].healthy = False
    result = await run(snapshot, fingerprint, timeout=5)
    assert result["status"] == "FAIL"
    failed_index = result["failed_index"]
    assert failed_index is not None
    assert result["completed"] == failed_index + 1
    # 失败即停：第二台设备的点读一步都不执行。
    assert fake_drivers["ads-010"].read_one_calls == 0
    assert fake_drivers["ads-001"].closes == 1
    assert fake_drivers["ads-010"].closes == 1
    failing_step = result["steps"][-1]
    assert failing_step["target"] == "ads-010"
    assert failing_step["code"] == "RuntimeError"


async def test_run_warning_quality_rolls_up_warning(config_dir, fake_drivers) -> None:
    from core.domain.sample_value import Quality

    snapshot, fingerprint = _snapshot(config_dir, "ads")
    fake_drivers["ads-001"].quality = Quality.BAD
    result = await run(snapshot, fingerprint, timeout=5)
    assert result["status"] == "WARNING"
    assert result["failed_index"] is None
    assert any(step["status"] == "WARNING" for step in result["steps"])


async def test_connect_error_is_fail_not_false_pass(config_dir, fake_drivers) -> None:
    snapshot, fingerprint = _snapshot(config_dir, "modbus")

    async def refuse() -> object:
        raise ConnectionError("unreachable")

    fake_drivers["modbus-001"].connect = refuse  # type: ignore[method-assign]
    result = await run(snapshot, fingerprint, timeout=5)
    assert result["status"] == "FAIL"
    assert result["steps"][0]["code"] == "ConnectionError"


async def test_driver_creation_failure_is_fail(config_dir, fake_drivers, monkeypatch) -> None:
    snapshot, fingerprint = _snapshot(config_dir, "modbus")
    import cli.pipeline as pipeline_mod

    def boom(snapshot: object, device_id: str) -> object:
        raise RuntimeError("unsupported protocol")

    monkeypatch.setattr(pipeline_mod, "create_driver", boom)
    result = await run(snapshot, fingerprint, timeout=5)
    assert result["status"] == "FAIL"
    assert result["failed_index"] == 0


# ---- 断点恢复 ----


def _previous(result: dict, failed_index: int) -> dict:
    previous = copy.deepcopy(result)
    previous["failed_index"] = failed_index
    previous["steps"] = copy.deepcopy(result["steps"][:failed_index])
    return previous


async def test_resume_skips_completed_steps(config_dir, fake_drivers) -> None:
    snapshot, fingerprint = _snapshot(config_dir, "ads")
    # 首跑：第二台设备 check 失败。
    fake_drivers["ads-010"].healthy = False
    first = await run(snapshot, fingerprint, timeout=5)
    assert first["status"] == "FAIL"
    failed_index = first["failed_index"]
    assert failed_index is not None
    previous = _previous(first, failed_index)

    # 恢复：修好第二台设备；第一台不应再被访问。
    fake_drivers["ads-001"].read_one_calls = 0
    fake_drivers["ads-001"].connects = 0
    fake_drivers["ads-010"].connects = 0
    fake_drivers["ads-010"].healthy = True
    second = await run(snapshot, fingerprint, previous=previous, timeout=5)
    assert second["status"] == "PASS"
    assert fake_drivers["ads-001"].connects == 0
    assert fake_drivers["ads-001"].read_one_calls == 0
    assert fake_drivers["ads-010"].connects == 1
    # 恢复结果包含首跑已完成步骤。
    assert second["completed"] == second["total"]


async def test_resume_restarts_on_fingerprint_change(config_dir, fake_drivers) -> None:
    snapshot, fingerprint = _snapshot(config_dir, "ads")
    fake_drivers["ads-010"].healthy = False
    first = await run(snapshot, fingerprint, timeout=5)
    previous = _previous(first, first["failed_index"])
    fake_drivers["ads-010"].healthy = True
    # 配置变化（指纹不同）必须从头重诊，不复用旧断点。
    second = await run(snapshot, "different-fingerprint", previous=previous, timeout=5)
    assert second["status"] == "PASS"
    assert fake_drivers["ads-001"].connects == 2


async def test_resume_restart_flag_forces_full_rerun(config_dir, fake_drivers) -> None:
    snapshot, fingerprint = _snapshot(config_dir, "ads")
    fake_drivers["ads-010"].healthy = False
    first = await run(snapshot, fingerprint, timeout=5)
    previous = _previous(first, first["failed_index"])
    fake_drivers["ads-010"].healthy = True
    second = await run(snapshot, fingerprint, previous=previous, restart=True, timeout=5)
    assert second["status"] == "PASS"
    assert fake_drivers["ads-001"].connects == 2


async def test_resume_ignores_tampered_step_history(config_dir, fake_drivers) -> None:
    snapshot, fingerprint = _snapshot(config_dir, "ads")
    planned = plan(snapshot)
    tampered = [{"kind": "point_read", "target": "ghost", "point": "x"}]
    start, steps = _resume(
        planned,
        fingerprint,
        {
            "fingerprint": fingerprint,
            "failed_index": 1,
            "steps": tampered,
        },
        False,
    )
    assert start == 0 and steps == []


def test_resume_validates_failed_index_bounds(config_dir) -> None:
    snapshot, _ = _snapshot(config_dir, "ads")
    planned = plan(snapshot)
    previous = {"fingerprint": "x", "failed_index": 999, "steps": []}
    start, _ = _resume(planned, "x", previous, False)
    assert start == 0


# ---- Sink 探测与 UNKNOWN 语义 ----


def test_sink_address_parsing() -> None:
    assert _sink_address({"connection": {"host": "h", "port": 1}}) == ("h", 1)
    assert _sink_address({"connection": {"bootstrap_servers": "kafka:9092,other:9093"}}) == (
        "kafka",
        9092,
    )
    assert _sink_address({"connection": {"endpoint": "grpc:50073"}}) == ("grpc", 50073)
    assert _sink_address({"connection": {"endpoint": "no-port"}}) is None
    assert _sink_address({"connection": {}}) is None


async def test_sink_ambiguous_endpoint_is_unknown_not_fail(
    config_dir, fake_drivers, monkeypatch
) -> None:
    snapshot, fingerprint = _snapshot(config_dir, "modbus")
    # Sink 配置无明确 TCP endpoint（如纯内存/凭据型）：UNKNOWN 而非 FAIL/PASS。
    monkeypatch.setattr("cli.pipeline._sink_address", lambda sink: None)
    result = await run(snapshot, fingerprint, timeout=5)
    sink_steps = [s for s in result["steps"] if s["kind"] == "sink_endpoint"]
    assert sink_steps and sink_steps[0]["status"] == "UNKNOWN"
    assert result["status"] == "UNKNOWN"


async def test_sink_tcp_probe_success(config_dir, fake_drivers, monkeypatch) -> None:
    from cli import pipeline as pipeline_mod

    async def fake_tcp(host: str, port: int, *, timeout: float) -> dict:
        return {"status": "PASS", "host": host, "port": port, "elapsed_ms": 0.1}

    monkeypatch.setattr(pipeline_mod, "tcp", fake_tcp)
    snapshot, fingerprint = _snapshot(config_dir, "modbus")
    result = await run(snapshot, fingerprint, timeout=5)
    assert result["status"] == "PASS"
    sink_step = next(s for s in result["steps"] if s["kind"] == "sink_endpoint")
    assert sink_step["data"]["status"] == "PASS"


async def test_timeout_bounds_validated(config_dir, fake_drivers) -> None:
    snapshot, fingerprint = _snapshot(config_dir, "modbus")
    with pytest.raises(ValueError):
        await run(snapshot, fingerprint, timeout=0)
    with pytest.raises(ValueError):
        await run(snapshot, fingerprint, timeout=301)


# ---- P2-2：Sink 检查完整性（不得静默遗漏、不得误报 PASS）----


def _write_sinks(base, content: str) -> Path:
    (base / "sinks.yaml").write_text(content, encoding="utf-8")
    return base


_ALL_OUTBOUND = """\
sinks:
  redis_latest:
    type: redis
    enabled: true
    connection: {host: 127.0.0.1, port: 6379}
  kafka_main:
    type: kafka
    enabled: true
    connection: {bootstrap_servers: "127.0.0.1:9092", topic: telemetry}
  tdengine_main:
    type: tdengine
    enabled: true
    connection: {host: 127.0.0.1, port: 6041, password: secret}
  grpc_gateway:
    type: grpc
    enabled: true
    connection: {endpoint: "127.0.0.1:50073"}
"""

_MIXED_REDIS_MODBUS = """\
sinks:
  redis_latest:
    type: redis
    enabled: true
    connection: {host: 127.0.0.1, port: 6379}
  modbus_inbound:
    type: modbus
    enabled: true
    connection: {host: 127.0.0.1, port: 15020}
"""

_ALL_DISABLED = """\
sinks:
  redis_latest:
    type: redis
    enabled: false
    connection: {host: 127.0.0.1, port: 6379}
  modbus_inbound:
    type: modbus
    enabled: false
    connection: {host: 127.0.0.1, port: 15020}
"""


def _snapshot_from(base):
    loaded = ConfigSession().read(base)
    return loaded.snapshot, loaded.fingerprint


def test_plan_covers_all_four_outbound_sink_kinds(config_dir) -> None:
    base = _write_sinks(config_dir("modbus"), _ALL_OUTBOUND)
    snapshot, _ = _snapshot_from(base)
    sink_steps = [step for step in plan(snapshot) if step["kind"].startswith("sink")]
    by_kind: dict[str, str] = {step["target"]: step["kind"] for step in sink_steps}
    assert by_kind == {
        "redis_latest": "sink_endpoint",
        "kafka_main": "sink_endpoint",
        "tdengine_main": "sink_endpoint",
        "grpc_gateway": "sink_endpoint",
    }


def test_plan_marks_modbus_inbound_sink_not_tested(config_dir) -> None:
    base = _write_sinks(config_dir("modbus"), _MIXED_REDIS_MODBUS)
    snapshot, _ = _snapshot_from(base)
    sink_plan = [step for step in plan(snapshot) if step["kind"].startswith("sink")]
    by_target = {step["target"]: step["kind"] for step in sink_plan}
    assert by_target["redis_latest"] == "sink_endpoint"
    # Modbus 入站 Sink 是监听方，出站 TCP 探针不适用：显式标记而非静默丢弃。
    assert by_target["modbus_inbound"] == "sink_not_tested"


def test_plan_excludes_disabled_sinks(config_dir) -> None:
    base = _write_sinks(config_dir("modbus"), _ALL_DISABLED)
    snapshot, _ = _snapshot_from(base)
    assert not [step for step in plan(snapshot) if step["kind"].startswith("sink")]


def test_unknown_sink_type_rejected_at_config_load(config_dir) -> None:
    # 白名单外类型（如未来的 nats）在配置加载阶段 fail-closed，
    # 不可能带着“未检查”的 Sink 进入诊断计划。
    from core.application.errors import ConfigError

    base = _write_sinks(
        config_dir("modbus"),
        """\
sinks:
  future_bus:
    type: nats
    enabled: true
    connection: {servers: "127.0.0.1:4222"}
""",
    )
    with pytest.raises(ConfigError, match="future_bus"):
        ConfigSession().read(base)


async def test_run_all_outbound_sinks_probe_tcp_and_pass(
    config_dir, fake_drivers, monkeypatch
) -> None:
    from cli import pipeline as pipeline_mod

    probes: list[tuple[str, int]] = []

    async def fake_tcp(host: str, port: int, *, timeout: float) -> dict:
        probes.append((host, port))
        return {"status": "PASS", "host": host, "port": port, "elapsed_ms": 0.1}

    monkeypatch.setattr(pipeline_mod, "tcp", fake_tcp)
    base = _write_sinks(config_dir("modbus"), _ALL_OUTBOUND)
    snapshot, fingerprint = _snapshot_from(base)
    result = await run(snapshot, fingerprint, timeout=5)
    assert result["status"] == "PASS"
    sink_steps = [step for step in result["steps"] if step["kind"].startswith("sink")]
    assert {step["status"] for step in sink_steps} == {"PASS"}
    assert ("127.0.0.1", 6041) in probes and ("127.0.0.1", 50073) in probes
    # scope/limitations 必须诚实声明只验证 TCP，不宣称数据交付。
    assert "outbound Sink TCP endpoints" in result["scope"]
    assert "does not verify data delivery" in result["limitations"]
    for step in sink_steps:
        # 探针结果只有连通性字段，不得出现交付/写入成功语义。
        assert set(step["data"]) >= {"status", "host", "port"}
        assert "delivered" not in step["data"]


async def test_run_modbus_inbound_sink_is_unknown_not_false_pass(
    config_dir, fake_drivers, monkeypatch
) -> None:
    from cli import pipeline as pipeline_mod

    probed: list[tuple[str, int]] = []

    async def fake_tcp(host: str, port: int, *, timeout: float) -> dict:
        probed.append((host, port))
        return {"status": "PASS", "host": host, "port": port, "elapsed_ms": 0.1}

    monkeypatch.setattr(pipeline_mod, "tcp", fake_tcp)
    base = _write_sinks(config_dir("modbus"), _MIXED_REDIS_MODBUS)
    snapshot, fingerprint = _snapshot_from(base)
    result = await run(snapshot, fingerprint, timeout=5)
    # redis 出站探针照常执行；入站监听端口 15020 绝不被探。
    assert probed == [("127.0.0.1", 6379)]
    # 存在未覆盖的已启用 Sink：整体只能 UNKNOWN，绝不允许 PASS。
    assert result["status"] == "UNKNOWN"
    inbound = next(step for step in result["steps"] if step["target"] == "modbus_inbound")
    assert inbound["kind"] == "sink_not_tested"
    assert inbound["status"] == "UNKNOWN"
    assert "modbus" in inbound["message"]
    redis_step = next(step for step in result["steps"] if step["target"] == "redis_latest")
    assert redis_step["kind"] == "sink_endpoint"


async def test_run_all_sinks_disabled_has_no_sink_steps(config_dir, fake_drivers) -> None:
    base = _write_sinks(config_dir("modbus"), _ALL_DISABLED)
    snapshot, fingerprint = _snapshot_from(base)
    result = await run(snapshot, fingerprint, timeout=5)
    assert result["status"] == "PASS"
    assert not [step for step in result["steps"] if step["kind"].startswith("sink")]


# ---- shell 集成：结果与报告 ----


def test_shell_diagnose_result_empty(config_dir, run_cli) -> None:
    base = config_dir("modbus")
    rc, out, _ = run_cli(["--config", str(base), "--format", "json", "diagnose", "result"])
    assert rc == 3
    assert "NO_DIAGNOSIS_RESULT" in out


def test_shell_run_then_result_and_report(config_dir, fake_drivers, run_cli, tmp_path) -> None:
    base = config_dir("modbus")
    rc, out, _ = run_cli(["--config", str(base), "--format", "json", "diagnose"])
    assert rc == 0, out
    assert '"status": "PASS"' in out
    # result 仅会话内有效：新进程拿不到（无持久化）。
    rc, out, _ = run_cli(["--config", str(base), "--format", "json", "diagnose", "result"])
    assert rc == 3


def test_shell_diagnose_single_shell_full_flow(config_dir, fake_drivers, tmp_path) -> None:
    import json

    shell = WindHubShell()
    shell.config.read(config_dir("modbus"))
    result = shell.dispatch(["diagnose"])
    assert isinstance(result, dict) and result["status"] == "PASS"
    again = shell.dispatch(["diagnose", "result"])
    assert again is result
    report_path = tmp_path / "evidence.json"
    export = shell.dispatch(["diagnose", "report", str(report_path)])
    assert isinstance(export, dict) and export["status"] == "PASS"
    payload = json.loads(report_path.read_text())
    assert payload["diagnosis"]["fingerprint"] == result["fingerprint"]
    # 原子导出不留临时文件。
    assert not list(tmp_path.glob(".*.tmp"))
    with pytest.raises(ValueError, match="\\.json"):
        shell.dispatch(["diagnose", "report", str(tmp_path / "report.txt")])
    missing = tmp_path / "no-dir" / "r.json"
    with pytest.raises(ValueError, match="directory"):
        shell.dispatch(["diagnose", "report", str(missing)])
