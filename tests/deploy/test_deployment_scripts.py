"""部署配置、READY 判定与自有资源回滚的离线契约测试。"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


@pytest.fixture
def scripts(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.syspath_prepend(str(REPO / "deploy"))
    return SimpleNamespace(
        render=importlib.import_module("render_cli"),
        collectors=importlib.import_module("prepare_collectors"),
        provision=importlib.import_module("provision"),
        ready=importlib.import_module("check_ready"),
    )


def _configs(root: Path, scripts: SimpleNamespace, *, process: bool) -> None:
    services = root / "configs" / "services"
    services.mkdir(parents=True)
    (root / "configs" / "instances").mkdir()
    (root / "configs" / "instances" / "site.yaml").write_text(
        "schema_version: 1\n", encoding="utf-8"
    )
    (root / "configs" / "templates").mkdir()
    sources = {
        "instances": {"site": "configs/instances/site.yaml"},
        "templates": {"root": "configs/templates"},
    }
    documents = {
        "gateway": {"grpc": {"listen_port": 50073}},
        "data-generator": {"service": {"grpc_bind": "127.0.0.1:50061"}},
        "collector_realtime": {"messaging": {"delivery": "realtime"}},
        "collector_reliable": {"messaging": {"delivery": "reliable"}},
        "sink": {
            "grpc": {"listen_port": 50053},
            "config_root": str(root) if process else "/etc/wind-hub",
        },
        "commander": {"grpc": {"listen_port": 50052}},
    }
    for name, body in documents.items():
        document = {
            "schema_version": 1,
            "config_sources": sources,
            **body,
        }
        (services / scripts.render.DEFAULT_FILES[name]).write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )


def _credentials(root: Path) -> tuple[Path, Path, Path]:
    certs = root / "certs"
    certs.mkdir()
    paths = (certs / "ca.pem", certs / "cli.pem", certs / "cli.key")
    for path in paths:
        path.write_text("test-only-non-credential-placeholder", encoding="utf-8")
        path.chmod(0o600)
    return paths


def _render_args(root: Path, scripts: SimpleNamespace, mode: str) -> argparse.Namespace:
    ca, cert, key = _credentials(root)
    collector_env = root / "collector.env"
    collector_env.write_text(
        f"WIND_HUB_COLLECTOR_TLS_CERT={cert}\n"
        f"WIND_HUB_COLLECTOR_TLS_KEY={key}\n"
        f"WIND_HUB_COLLECTOR_TLS_CA={ca}\n"
        "WIND_HUB_COLLECTOR_ADMIN_TOKEN=test-placeholder\n",
        encoding="utf-8",
    )
    collector_env.chmod(0o600)
    values: dict[str, Any] = {
        "root": root,
        "mode": mode,
        "python": Path(sys.executable),
        "namespace": "wind-hub-test",
        "socket": Path("/run/test/containerd.sock"),
        "isula": Path("/usr/bin/isula"),
        "ca": ca,
        "client_cert": cert,
        "client_key": key,
        "collector_env_file": collector_env if mode == "process" else None,
        "runtime_prefix": Path("/etc/wind-hub") if mode == "rpc" else None,
    }
    values.update(
        {name.replace("-", "_"): value for name, value in scripts.render.DEFAULT_FILES.items()}
    )
    return argparse.Namespace(**values)


@pytest.mark.parametrize("mode", ["process", "containerd", "isulad", "rpc"])
def test_render_modes_keep_paths_and_managers_distinct(
    tmp_path: Path, scripts: SimpleNamespace, mode: str
) -> None:
    _configs(tmp_path, scripts, process=mode == "process")
    payload = scripts.render._render(_render_args(tmp_path, scripts, mode))

    gateway = payload["services"]["gateway"]
    assert gateway["grpc_endpoint"] == "127.0.0.1:50073"
    if mode == "process":
        assert gateway["manager"] == "process"
        assert gateway["process"]["argv"][:2] == [str(Path(sys.executable).resolve()), "-m"]
        assert gateway["process"]["log_file"] == str(tmp_path / "logs" / "gateway.log")
        assert payload["services"]["collector_reliable"]["process"]["argv"][-1] == "50054"
        assert set(payload["services"]["collector_realtime"]["process"]["env"]) == set(
            scripts.render.COLLECTOR_ENV_KEYS
        ) - {"WIND_HUB_COLLECTOR_ADMIN_TOKEN"}
        assert "test-placeholder" not in yaml.safe_dump(payload)
        assert "WIND_HUB_COLLECTOR_ADMIN_TOKEN" not in yaml.safe_dump(payload)
    elif mode == "containerd":
        assert gateway["manager"] == "containerd"
        assert gateway["target"] == "wind-hub-gateway"
        assert gateway["manager_socket"] == "/run/test/containerd.sock"
        assert "process" not in gateway
    elif mode == "isulad":
        assert gateway["manager"] == "isulad"
        assert gateway["isula_binary"] == "/usr/bin/isula"
        assert "process" not in gateway
    else:
        assert "manager" not in gateway
        assert payload["tls_profiles"]["local"]["client_key"] == "/etc/wind-hub/certs/cli.key"
    assert "manager" not in payload["services"]["collector"]


def test_render_rejects_malformed_nested_config(tmp_path: Path, scripts: SimpleNamespace) -> None:
    _configs(tmp_path, scripts, process=True)
    path = tmp_path / "configs" / "services" / "data-generator-sim.example.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["service"] = ["not-a-mapping"]
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="service must be a mapping"):
        scripts.render._render(_render_args(tmp_path, scripts, "process"))


def test_render_rejects_missing_config_sources(tmp_path: Path, scripts: SimpleNamespace) -> None:
    _configs(tmp_path, scripts, process=True)
    path = tmp_path / "configs" / "services" / "commander.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["config_sources"]["templates"] = {}
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="templates must be a non-empty mapping"):
        scripts.render._render(_render_args(tmp_path, scripts, "process"))


def test_process_requires_complete_collector_mtls_environment(
    tmp_path: Path, scripts: SimpleNamespace
) -> None:
    _configs(tmp_path, scripts, process=True)
    args = _render_args(tmp_path, scripts, "process")
    args.collector_env_file = None
    with pytest.raises(ValueError, match="collector-env-file"):
        scripts.render._render(args)


def test_compose_collector_identity_and_tls_match_native_contract(
    scripts: SimpleNamespace,
) -> None:
    compose = yaml.safe_load((REPO / "deploy" / "compose.yaml").read_text(encoding="utf-8"))
    for service in ("collector-realtime", "collector-reliable"):
        expected = service.replace("-", "_")
        row = compose["services"][service]
        command = row["command"]
        assert command[command.index("--collector-id") + 1] == expected
        assert command[command.index("--state-directory") + 1].endswith(expected)
        environment = row["environment"]
        assert set(environment) == scripts.render.COLLECTOR_ENV_KEYS


def test_render_rejects_group_readable_private_key(
    tmp_path: Path, scripts: SimpleNamespace
) -> None:
    _configs(tmp_path, scripts, process=True)
    args = _render_args(tmp_path, scripts, "process")
    args.client_key.chmod(0o640)
    with pytest.raises(ValueError, match="client key"):
        scripts.render._render(args)


def test_collector_pair_rolls_back_only_new_files(
    tmp_path: Path, scripts: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = tmp_path / "configs" / "services"
    services.mkdir(parents=True)
    (services / "collector.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "messaging": {"servers": ["nats:4222"]}}),
        encoding="utf-8",
    )
    unrelated = services / "collector-realtime.yaml.unrelated"
    unrelated.write_text("keep", encoding="utf-8")
    original = scripts.collectors._create
    calls = 0

    def fail_second(path: Path, document: dict[str, Any], created: list[Path]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected interruption")
        original(path, document, created)

    monkeypatch.setattr(scripts.collectors, "_create", fail_second)
    with pytest.raises(SystemExit) as raised:
        scripts.collectors.main(["--root", str(tmp_path)])
    assert raised.value.code == 2
    assert not (services / "collector-realtime.yaml").exists()
    assert not (services / "collector-reliable.yaml").exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_collector_pair_removes_partially_written_file(
    tmp_path: Path, scripts: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = tmp_path / "configs" / "services"
    services.mkdir(parents=True)
    (services / "collector.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "messaging": {"servers": ["nats:4222"]}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        scripts.collectors.yaml,
        "safe_dump",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected write failure")),
    )
    with pytest.raises(SystemExit):
        scripts.collectors.main(["--root", str(tmp_path)])
    assert not (services / "collector-realtime.yaml").exists()
    assert not (services / "collector-reliable.yaml").exists()


@pytest.mark.parametrize(
    ("process", "expected"),
    [
        ({"status": "UNMANAGED"}, True),
        ({"manager": "containerd", "state": "RUNNING"}, True),
        ({"manager": "containerd", "state": "CREATED"}, False),
        ({"manager": "containerd", "state": "PAUSED"}, False),
        ({"manager": "process", "state": "RUNNING", "running": True}, True),
        ({"manager": "process", "state": "STOPPED", "running": False}, False),
        ({"manager": "isulad", "state": "running", "running": True}, True),
        ({"manager": "isulad", "state": "exited", "running": False}, False),
        ({}, False),
    ],
)
def test_ready_requires_live_managed_process_or_explicit_rpc_only(
    scripts: SimpleNamespace, process: dict[str, Any], expected: bool
) -> None:
    state = {"business": {"health": "READY"}, "process": process}
    assert scripts.ready._ready(state) is expected


def test_ready_rejects_zero_exit_non_success_envelope(
    scripts: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    envelope = {"status": "PARTIAL", "data": [{"service": "gateway"}]}
    monkeypatch.setattr(
        scripts.ready.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(envelope), stderr=""
        ),
    )
    with pytest.raises(ValueError, match="not SUCCESS"):
        scripts.ready._status(tmp_path / "cli.yaml", "gateway", 1.0)


def _provision_args(root: Path, scripts: SimpleNamespace, runtime: str) -> argparse.Namespace:
    env_file = root / "collector-test.env"
    env_file.write_text(
        "WIND_HUB_COLLECTOR_TLS_CERT=/etc/wind-hub/certs/collector.pem\n"
        "WIND_HUB_COLLECTOR_TLS_KEY=/etc/wind-hub/certs/collector.key\n"
        "WIND_HUB_COLLECTOR_TLS_CA=/etc/wind-hub/certs/ca.pem\n"
        "WIND_HUB_COLLECTOR_ADMIN_TOKEN=test-placeholder\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    return argparse.Namespace(
        root=root,
        image="wind-hub:test",
        uid=1000,
        gid=1000,
        runtime=runtime,
        service="all",
        collector_env_file=env_file,
        ctr="ctr",
        socket=Path("/run/test/containerd.sock"),
        namespace="wind-hub-test",
        isula=Path("/usr/bin/isula"),
        apply=True,
    )


@pytest.mark.parametrize("runtime", ["containerd", "isulad"])
def test_provision_preview_constructs_isolated_non_root_commands(
    tmp_path: Path, scripts: SimpleNamespace, runtime: str
) -> None:
    _configs(tmp_path, scripts, process=False)
    args = _provision_args(tmp_path, scripts, runtime)
    commands = scripts.provision._build(args)
    expected_count = 12 if runtime == "containerd" else 6
    assert len(commands) == expected_count
    rendered = [str(part) for command in commands for part in command]
    assert "65532" not in rendered
    assert "1000:1000" in rendered
    assert f"type=bind,src={tmp_path},dst=/etc/wind-hub,options=rbind:rw" in rendered or (
        f"{tmp_path}:/etc/wind-hub:rw" in rendered
    )
    assert not any("systemd" in part for part in rendered)


@pytest.mark.parametrize("runtime", ["containerd", "isulad"])
def test_provision_failure_rolls_back_only_successfully_created_ids(
    tmp_path: Path,
    scripts: SimpleNamespace,
    runtime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configs(tmp_path, scripts, process=False)
    args = _provision_args(tmp_path, scripts, runtime)
    commands = scripts.provision._build(args)[:3]
    calls: list[list[str]] = []
    original_create_calls = 0

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal original_create_calls
        calls.append(command)
        if scripts.provision._created_target(args, command) is not None:
            original_create_calls += 1
            if original_create_calls == 2:
                raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(scripts.provision.subprocess, "run", fake_run)
    monkeypatch.setattr(scripts.provision, "_inspect_target", lambda _args, _id: False)
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        scripts.provision._apply(args, commands)

    removals = [command for command in calls if "rm" in command]
    assert removals
    assert all("wind-hub-data-generator" not in command for command in removals)
    assert any("wind-hub-gateway" in command for command in removals)
    assert all("unrelated-user-container" not in command for command in calls)


def test_runtime_result_rejects_created_and_paused_containers() -> None:
    from cli.runtime import Runtime

    for state in ("CREATED", "PAUSED", "PAUSING", "STOPPED"):
        row = {
            "process": {"manager": "containerd", "state": state},
            "business": {"health": "READY"},
        }
        assert Runtime._result_state("service status", [row]) == ("UNKNOWN", 3)
    running = {
        "process": {"manager": "containerd", "state": "RUNNING"},
        "business": {"health": "READY"},
    }
    unmanaged = {"process": {"status": "UNMANAGED"}, "business": {"health": "READY"}}
    assert Runtime._result_state("service status", [running, unmanaged]) == ("SUCCESS", 0)
