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
        provision=importlib.import_module("provision"),
        ready=importlib.import_module("check_ready"),
    )


def _configs(root: Path, scripts: SimpleNamespace, *, process: bool) -> None:
    services = root / "configs" / "services"
    services.mkdir(parents=True)
    instances = root / "configs" / "instances"
    instances.mkdir()
    (instances / "site_example_site_v1.0.0.yaml").write_text(
        "schema_version: 1\nfile_version: '1.0.0'\n", encoding="utf-8"
    )
    (root / "configs" / "templates").mkdir()
    selected = {
        "gateway": "gateway",
        "data-generator": "data-generator",
        "collector_realtime": "collector-realtime",
        "collector_reliable": "collector-reliable",
        "sink": "sink-west",
        "commander": "commander",
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
        identity = selected[name]
        document = {
            "schema_version": 1,
            "file_version": "1.0.0",
            "service_id": identity,
            "config_sources": {"instances": {"site": "example_site"}},
            **body,
        }
        if name in {"gateway", "commander", "data-generator"}:
            roles = {
                "gateway": (("operator", "WIND_HUB_TEST_GATEWAY_OPERATOR_TOKEN"),),
                "commander": (
                    ("ems", "WIND_HUB_TEST_COMMANDER_EMS_TOKEN"),
                    ("maintenance", "WIND_HUB_TEST_COMMANDER_MAINTENANCE_TOKEN"),
                ),
                "data-generator": (("cli", "WIND_HUB_TEST_GENERATOR_CLI_TOKEN"),),
            }
            document["schema_version"] = 2 if name == "data-generator" else 3
            document["security"] = {
                "callers": [
                    {"caller_id": caller, "token_env": env, "permissions": ["read"]}
                    for caller, env in roles[name]
                ]
            }
        (services / f"service_{identity}_v1.0.0.yaml").write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )
    deployments = root / "configs" / "deployments"
    deployments.mkdir()
    deployment = {
        "schema_version": 1,
        "file_version": "1.3.0",
        "deployment_id": "prod-a",
        "services": {
            identity: {"config_id": identity, "file_version": "1.0.0"}
            for identity in selected.values()
        },
        "instances": {"site": {"example_site": {"file_version": "1.0.0"}}},
    }
    (deployments / "deployment_prod-a_v1.3.0.yaml").write_text(
        yaml.safe_dump(deployment), encoding="utf-8"
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
    values["deployment"] = "configs/deployments/deployment_prod-a_v1.3.0.yaml"
    values.update({name.replace("-", "_"): None for name in scripts.render.SERVICE_NAMES})
    return argparse.Namespace(**values)


@pytest.mark.parametrize("mode", ["process", "containerd", "isulad", "rpc"])
def test_render_modes_keep_paths_and_managers_distinct(
    tmp_path: Path, scripts: SimpleNamespace, mode: str
) -> None:
    _configs(tmp_path, scripts, process=mode == "process")
    payload = scripts.render._render(_render_args(tmp_path, scripts, mode))

    gateway = payload["services"]["gateway"]
    assert gateway["grpc_endpoint"] == "127.0.0.1:50073"
    assert gateway["token_env"] == "WIND_HUB_TEST_GATEWAY_OPERATOR_TOKEN"
    assert "tls_profile" not in gateway
    assert payload["services"]["data-generator"]["token_env"] == (
        "WIND_HUB_TEST_GENERATOR_CLI_TOKEN"
    )
    assert payload["services"]["commander"]["method_token_envs"] == {
        "RecoverDeviceControl": "WIND_HUB_TEST_COMMANDER_MAINTENANCE_TOKEN"
    }
    if mode == "process":
        assert gateway["manager"] == "process"
        assert gateway["process"]["argv"][:2] == [str(Path(sys.executable).resolve()), "-m"]
        assert gateway["process"]["log_file"] == str(tmp_path / "logs" / "gateway.log")
        assert payload["services"]["collector_reliable"]["process"]["argv"][-1] == "50054"
        assert set(payload["services"]["collector_realtime"]["process"]["env"]) == (
            set(scripts.render.COLLECTOR_ENV_KEYS) - {"WIND_HUB_COLLECTOR_ADMIN_TOKEN"}
            | {"WIND_HUB_DEPLOYMENT_ID", "WIND_HUB_DEPLOYMENT_VERSION"}
        )
        assert payload["services"]["gateway"]["process"]["env"] == {
            "WIND_HUB_DEPLOYMENT_ID": "prod-a",
            "WIND_HUB_DEPLOYMENT_VERSION": "1.3.0",
        }
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
    if mode == "process":
        assert payload["services"]["sink"]["process"]["argv"][-2:] == [
            "--service-file",
            "configs/services/service_sink-west_v1.0.0.yaml",
        ]


def test_render_rejects_service_override_outside_deployment(
    tmp_path: Path, scripts: SimpleNamespace
) -> None:
    _configs(tmp_path, scripts, process=True)
    args = _render_args(tmp_path, scripts, "process")
    selected = tmp_path / "configs" / "services" / "service_gateway_v1.0.0.yaml"
    alternate = selected.with_name("service_gateway_copy_v1.0.0.yaml")
    alternate.write_bytes(selected.read_bytes())
    args.gateway = alternate.name
    with pytest.raises(ValueError, match="Service override differs from selected Deployment"):
        scripts.render._render(args)


def _native_bootstraps(root: Path, args: argparse.Namespace) -> None:
    configuration = root / "configuration_bootstrap.yaml"
    storage = root / "storage_bootstrap.yaml"
    configuration.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "grpc": {"host": "127.0.0.1", "port": 51060},
            }
        ),
        encoding="utf-8",
    )
    storage.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "service_id": "storage-main",
                "deployment_id": "prod-a",
                "configuration": {"endpoint": "127.0.0.1:51060"},
            }
        ),
        encoding="utf-8",
    )
    deployment_path = root / "configs" / "deployments" / "deployment_prod-a_v1.3.0.yaml"
    deployment = yaml.safe_load(deployment_path.read_text(encoding="utf-8"))
    deployment["services"]["storage-main"] = {"config_id": "storage-main", "file_version": "1.0.0"}
    deployment_path.write_text(yaml.safe_dump(deployment), encoding="utf-8")
    (root / "configs" / "services" / "service_storage-main_v1.0.0.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "file_version": "1.0.0",
                "service_id": "storage-main",
                "service": {"grpc": {"listen_host": "127.0.0.1", "listen_port": 51110}},
            }
        ),
        encoding="utf-8",
    )
    args.configuration_bootstrap = configuration
    args.configuration_endpoint = "127.0.0.1:51060"
    args.storage_bootstrap = storage
    args.storage_endpoint = "127.0.0.1:51110"


def test_native_render_includes_configuration_and_storage(
    tmp_path: Path, scripts: SimpleNamespace
) -> None:
    _configs(tmp_path, scripts, process=True)
    args = _render_args(tmp_path, scripts, "process")
    _native_bootstraps(tmp_path, args)
    result = scripts.render._render(args)
    from cli.runtime import load_config

    cli_file = tmp_path / "generated-cli.yaml"
    cli_file.write_text(yaml.safe_dump(result), encoding="utf-8")
    loaded = load_config(cli_file)
    assert {"configuration", "storage"} <= set(loaded.services)
    for name, module, selector in (
        ("configuration", "configuration_service", "--config"),
        ("storage", "storage_service.main", "--bootstrap"),
    ):
        row = result["services"][name]
        assert row["manager"] == "process"
        assert row["process"]["argv"][1:4] == ["-m", module, selector]
        assert row["process"]["env"] == {
            "WIND_HUB_DEPLOYMENT_ID": "prod-a",
            "WIND_HUB_DEPLOYMENT_VERSION": "1.3.0",
        }
        assert "tls_profile" not in row


def test_native_render_rejects_inconsistent_storage_dependency(
    tmp_path: Path, scripts: SimpleNamespace
) -> None:
    _configs(tmp_path, scripts, process=True)
    args = _render_args(tmp_path, scripts, "process")
    _native_bootstraps(tmp_path, args)
    data = yaml.safe_load(args.storage_bootstrap.read_text(encoding="utf-8"))
    data["configuration"]["endpoint"] = "127.0.0.1:59999"
    args.storage_bootstrap.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="storage configuration endpoint differs"):
        scripts.render._render(args)


def test_native_storage_rejects_unpublished_selected_service(
    tmp_path: Path, scripts: SimpleNamespace
) -> None:
    _configs(tmp_path, scripts, process=True)
    args = _render_args(tmp_path, scripts, "process")
    _native_bootstraps(tmp_path, args)
    (tmp_path / "configs" / "services" / "service_storage-main_v1.0.0.yaml").unlink()
    with pytest.raises(ValueError, match="missing or symlinked service YAML"):
        scripts.render._render(args)


def test_native_storage_rejects_service_listener_drift(
    tmp_path: Path, scripts: SimpleNamespace
) -> None:
    _configs(tmp_path, scripts, process=True)
    args = _render_args(tmp_path, scripts, "process")
    _native_bootstraps(tmp_path, args)
    args.storage_endpoint = "127.0.0.1:51111"
    with pytest.raises(ValueError, match="storage CLI endpoint differs"):
        scripts.render._render(args)


def test_native_configuration_rejects_endpoint_drift(
    tmp_path: Path, scripts: SimpleNamespace
) -> None:
    _configs(tmp_path, scripts, process=True)
    args = _render_args(tmp_path, scripts, "process")
    _native_bootstraps(tmp_path, args)
    args.configuration_endpoint = "127.0.0.1:51061"
    with pytest.raises(ValueError, match="configuration bootstrap listener differs"):
        scripts.render._render(args)


def test_additional_service_bootstraps_are_process_only(
    tmp_path: Path, scripts: SimpleNamespace
) -> None:
    _configs(tmp_path, scripts, process=False)
    args = _render_args(tmp_path, scripts, "rpc")
    args.configuration_bootstrap = tmp_path / "config.yaml"
    args.configuration_endpoint = "127.0.0.1:51060"
    with pytest.raises(ValueError, match="require process mode"):
        scripts.render._render(args)


def test_render_rejects_malformed_nested_config(tmp_path: Path, scripts: SimpleNamespace) -> None:
    _configs(tmp_path, scripts, process=True)
    path = tmp_path / "configs" / "services" / "service_data-generator_v1.0.0.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["service"] = ["not-a-mapping"]
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="service must be a mapping"):
        scripts.render._render(_render_args(tmp_path, scripts, "process"))


def test_render_rejects_missing_config_sources(tmp_path: Path, scripts: SimpleNamespace) -> None:
    _configs(tmp_path, scripts, process=True)
    path = tmp_path / "configs" / "services" / "service_commander_v1.0.0.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["config_sources"]["instances"] = None
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="instances must be a mapping"):
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
        assert set(environment) == scripts.render.COLLECTOR_ENV_KEYS | {
            "WIND_HUB_DEPLOYMENT_ID",
            "WIND_HUB_DEPLOYMENT_VERSION",
        }


def test_render_rejects_group_readable_private_key(
    tmp_path: Path, scripts: SimpleNamespace
) -> None:
    _configs(tmp_path, scripts, process=True)
    args = _render_args(tmp_path, scripts, "process")
    args.client_key.chmod(0o640)
    with pytest.raises(ValueError, match="client key"):
        scripts.render._render(args)


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
        deployment="configs/deployments/deployment_prod-a_v1.3.0.yaml",
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


def test_inspect_target_classifies_present_absent_and_uncertain(
    tmp_path: Path,
    scripts: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configs(tmp_path, scripts, process=False)
    args = _provision_args(tmp_path, scripts, "containerd")
    ident = "wind-hub-gateway"

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command[-2:] == ["info", ident]
        return next(outcomes)

    outcomes = iter([subprocess.CompletedProcess([], 0, "{}", "")])
    monkeypatch.setattr(scripts.provision.subprocess, "run", fake_run)
    assert scripts.provision._inspect_target(args, ident) is True

    outcomes = iter(
        [
            subprocess.CompletedProcess([], 1, "", "container not found"),
            subprocess.CompletedProcess([], 1, "", "no such container: x"),
            subprocess.CompletedProcess([], 1, "", "object does not exist"),
        ]
    )
    for _ in range(3):
        assert scripts.provision._inspect_target(args, ident) is False

    outcomes = iter([subprocess.CompletedProcess([], 1, "", "permission denied on socket")])
    assert scripts.provision._inspect_target(args, ident) is None

    def raise_timeout(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 15)

    monkeypatch.setattr(scripts.provision.subprocess, "run", raise_timeout)
    assert scripts.provision._inspect_target(args, ident) is None

    def raise_oserror(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("socket vanished")

    monkeypatch.setattr(scripts.provision.subprocess, "run", raise_oserror)
    assert scripts.provision._inspect_target(args, ident) is None


def test_inspect_target_uses_isula_inspect_for_isulad_runtime(
    tmp_path: Path,
    scripts: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configs(tmp_path, scripts, process=False)
    args = _provision_args(tmp_path, scripts, "isulad")
    seen: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 1, "", "container not found")

    monkeypatch.setattr(scripts.provision.subprocess, "run", fake_run)
    assert scripts.provision._inspect_target(args, "wind-hub-gateway") is False
    assert seen == [["/usr/bin/isula", "inspect", "wind-hub-gateway"]]


@pytest.mark.parametrize("presence", [True, None])
def test_apply_refuses_when_precreate_target_exists_or_unverified(
    tmp_path: Path,
    scripts: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    presence: bool | None,
) -> None:
    _configs(tmp_path, scripts, process=False)
    args = _provision_args(tmp_path, scripts, "containerd")
    commands = scripts.provision._build(args)[:2]
    calls: list[list[str]] = []

    monkeypatch.setattr(scripts.provision, "_inspect_target", lambda _args, _id: presence)
    monkeypatch.setattr(
        scripts.provision.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command)
        or subprocess.CompletedProcess(command, 0, "", ""),
    )
    with pytest.raises(RuntimeError, match="refusing create"):
        scripts.provision._apply(args, commands)
    # The create command must never have run; no rollback either.
    assert calls == []


def test_apply_runs_without_rollback_when_all_creates_verified_absent(
    tmp_path: Path,
    scripts: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configs(tmp_path, scripts, process=False)
    args = _provision_args(tmp_path, scripts, "containerd")
    commands = scripts.provision._build(args)[:4]
    calls: list[list[str]] = []

    monkeypatch.setattr(scripts.provision, "_inspect_target", lambda _args, _id: False)
    monkeypatch.setattr(
        scripts.provision.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command)
        or subprocess.CompletedProcess(command, 0, "", ""),
    )
    scripts.provision._apply(args, commands)
    assert not any("rm" in command or "kill" in command for command in calls)
    create_targets = [
        scripts.provision._created_target(args, command)
        for command in calls
        if scripts.provision._created_target(args, command) is not None
    ]
    assert create_targets == ["wind-hub-gateway", "wind-hub-data-generator"]


@pytest.mark.parametrize(
    "failure",
    [
        lambda command: subprocess.TimeoutExpired(command, 120),
        lambda command: subprocess.CalledProcessError(1, command),
        KeyboardInterrupt,
        lambda command: OSError("broken pipe"),
    ],
)
def test_apply_unknown_create_result_never_deletes_unowned_target(
    tmp_path: Path,
    scripts: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure: Any,
) -> None:
    _configs(tmp_path, scripts, process=False)
    args = _provision_args(tmp_path, scripts, "containerd")
    commands = scripts.provision._build(args)[:2]
    calls: list[list[str]] = []
    target = scripts.provision._created_target(args, commands[0])

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if scripts.provision._created_target(args, command) is not None:
            raise failure(command) if callable(failure) else failure()
        return subprocess.CompletedProcess(command, 0, "", "")

    inspect_results = iter([False, True])
    monkeypatch.setattr(
        scripts.provision,
        "_inspect_target",
        lambda _args, _id: next(inspect_results),
    )
    monkeypatch.setattr(scripts.provision.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="UNKNOWN") as exc_info:
        scripts.provision._apply(args, commands)
    message = str(exc_info.value)
    assert target in message and "present=True" in message
    assert "NOT deleted" in message and "manual runtime reconciliation" in message
    # Nothing owned existed yet, so no removal is attempted.
    assert not any("rm" in command or "kill" in command for command in calls)


def test_apply_unknown_result_still_rolls_back_previously_owned_containers(
    tmp_path: Path,
    scripts: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configs(tmp_path, scripts, process=False)
    args = _provision_args(tmp_path, scripts, "containerd")
    commands = scripts.provision._build(args)[:4]
    calls: list[list[str]] = []
    create_seen = 0

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal create_seen
        calls.append(command)
        if scripts.provision._created_target(args, command) is not None:
            create_seen += 1
            if create_seen == 2:
                raise subprocess.TimeoutExpired(command, 120)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(scripts.provision, "_inspect_target", lambda _args, _id: False)
    monkeypatch.setattr(scripts.provision.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        scripts.provision._apply(args, commands)
    removals = [command for command in calls if "rm" in command]
    assert any("wind-hub-gateway" in command for command in removals)
    assert all("wind-hub-data-generator" not in command for command in removals)


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
