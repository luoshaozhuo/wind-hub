"""cli.yaml 独立加载与边界校验（设计 RR-01/CR-01～06）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.runtime import Runtime, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cli.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_BASE_HEADER = """
schema_version: 1
file_version: "1.0.0"
cli:
  timeout_seconds: 2
"""


def test_load_minimal_config(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, _BASE_HEADER))
    assert config.timeout == 2.0
    assert config.max_hosts == 1024
    assert config.industrial_ports == (102, 502, 2404, 4840, 44818, 48898)


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        load_config(tmp_path / "absent.yaml")


@pytest.mark.parametrize(
    "body",
    [
        "schema_version: 2\ncli: {}\n",
        "schema_version: '1'\n",
        "cli: {}\n",
    ],
)
def test_schema_version_must_be_integer_1(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValueError, match="schema_version"):
        load_config(_write(tmp_path, body))


def test_duplicate_yaml_keys_rejected(tmp_path: Path) -> None:
    body = """
schema_version: 1
file_version: "1.0.0"
cli:
  timeout_seconds: 1
  timeout_seconds: 2
"""
    with pytest.raises(ValueError, match="duplicate mapping key"):
        load_config(_write(tmp_path, body))


def test_rpc_via_is_not_supported(tmp_path: Path) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  sink_proxy:
    rpc_via: sink
"""
    )
    with pytest.raises(ValueError, match="rpc_via is not supported"):
        load_config(_write(tmp_path, body))


def test_loopback_collector_without_tls_profile(tmp_path: Path) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  collector:
    grpc_endpoint: "127.0.0.1:5000"
"""
    )
    assert load_config(_write(tmp_path, body)).services["collector"].tls_profile is None


def test_token_authenticated_loopback_service(tmp_path: Path) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  gateway:
    grpc_endpoint: "127.0.0.1:5000"
    token_env: WIND_HUB_GATEWAY_OPERATOR_TOKEN
"""
    )
    assert load_config(_write(tmp_path, body)).services["gateway"].token_env == (
        "WIND_HUB_GATEWAY_OPERATOR_TOKEN"
    )


@pytest.mark.parametrize(
    ("endpoint", "token_env", "error"),
    [
        ("192.168.1.20:5000", "WIND_HUB_GATEWAY_TOKEN", "loopback endpoint"),
        ("127.0.0.1:5000", None, "token_env is required"),
        ("127.0.0.1:5000", "invalid.token", "token_env must"),
        ("user@127.0.0.1:5000", "WIND_HUB_GATEWAY_TOKEN", "loopback endpoint"),
        ("127.0.0.1:5000/path", "WIND_HUB_GATEWAY_TOKEN", "loopback endpoint"),
        ("localhost:5000", "WIND_HUB_GATEWAY_TOKEN", "loopback endpoint"),
    ],
)
def test_loopback_authentication_fail_closed(
    tmp_path: Path, endpoint: str, token_env: str | None, error: str
) -> None:
    body = (
        _BASE_HEADER
        + f"""
services:
  gateway:
    grpc_endpoint: "{endpoint}"
"""
    )
    if token_env is not None:
        body += f"    token_env: {token_env}\n"
    with pytest.raises(ValueError, match=error):
        load_config(_write(tmp_path, body))


def test_unknown_tls_profile_reference_rejected(tmp_path: Path) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  collector:
    grpc_endpoint: "localhost:5000"
    tls_profile: missing
"""
    )
    with pytest.raises(ValueError, match="unknown tls_profile"):
        load_config(_write(tmp_path, body))


def test_containerd_requires_namespace_and_absolute_socket(tmp_path: Path) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  collector:
    manager: containerd
    target: wind-hub-collector
    namespace: default
    manager_socket: containerd.sock
"""
    )
    with pytest.raises(ValueError, match="manager_socket must be absolute"):
        load_config(_write(tmp_path, body))


def test_isulad_binary_must_be_absolute(tmp_path: Path) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  collector:
    manager: isulad
    target: wind-hub-collector
    isula_binary: isula
"""
    )
    with pytest.raises(ValueError, match="isula_binary must be absolute"):
        load_config(_write(tmp_path, body))


@pytest.mark.parametrize("field", ["cwd", "state_dir", "log_file"])
def test_process_paths_reject_traversal(tmp_path: Path, field: str) -> None:
    process = {
        "cwd": "/opt/wind-hub/src",
        "state_dir": "/var/lib/wind-hub/state",
        "log_file": "/var/log/wind-hub/service.log",
    }
    process[field] = "/tmp/../unsafe"
    body = (
        _BASE_HEADER
        + f"""
services:
  native:
    manager: process
    target: native
    process:
      argv: ["/usr/bin/python3", "-V"]
      cwd: {process["cwd"]}
      state_dir: {process["state_dir"]}
      log_file: {process["log_file"]}
"""
    )
    with pytest.raises(ValueError, match=field):
        load_config(_write(tmp_path, body))


@pytest.mark.parametrize(
    "ports",
    [
        [],
        [102, 102],
        [0],
        [70000],
        list(range(65)),
    ],
)
def test_industrial_port_list_bounds(tmp_path: Path, ports: list[int]) -> None:
    body = _BASE_HEADER + f"network_probe:\n  industrial_tcp_ports: {ports}\n"
    with pytest.raises(ValueError, match="industrial_tcp_ports"):
        load_config(_write(tmp_path, body))


@pytest.mark.parametrize("hosts", [0, 4097])
def test_max_hosts_hard_bounds(tmp_path: Path, hosts: int) -> None:
    body = _BASE_HEADER + f"network_probe:\n  max_hosts: {hosts}\n"
    with pytest.raises(ValueError, match="max_hosts"):
        load_config(_write(tmp_path, body))


@pytest.mark.parametrize(
    "file_version",
    [None, 1, "1", "v1.0.0", "1.0", "1.0.0-beta", "1..0"],
)
def test_file_version_is_required_semver_string(tmp_path: Path, file_version: object) -> None:
    document = "schema_version: 1\n"
    if file_version is not None:
        document += f"file_version: {file_version!r}\n"
    with pytest.raises(ValueError, match="file_version"):
        load_config(_write(tmp_path, document))


def test_loopback_service_passes_bearer_token_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  gateway:
    grpc_endpoint: "127.0.0.1:5000"
    token_env: WIND_HUB_TEST_GATEWAY_CLI_TOKEN
"""
    )
    runtime = Runtime(_write(tmp_path, body), None)
    with pytest.raises(PermissionError, match="credential missing"):
        runtime.get_rpc_metadata("gateway", "ReadVariables")
    monkeypatch.setenv("WIND_HUB_TEST_GATEWAY_CLI_TOKEN", "a" * 40)
    assert runtime.get_rpc_metadata("gateway", "ReadVariables") == (
        ("authorization", "Bearer " + "a" * 40),
    )
    monkeypatch.setenv("WIND_HUB_TEST_GATEWAY_CLI_TOKEN", "a" * 31)
    with pytest.raises(PermissionError, match="credential missing"):
        runtime.get_rpc_metadata("gateway", "ReadVariables")


def test_method_specific_caller_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  commander:
    grpc_endpoint: "127.0.0.1:50052"
    token_env: WIND_HUB_TEST_COMMANDER_EMS_TOKEN
    method_token_envs:
      RecoverDeviceControl: WIND_HUB_TEST_COMMANDER_MAINTENANCE_TOKEN
"""
    )
    runtime = Runtime(_write(tmp_path, body), None)
    monkeypatch.setenv("WIND_HUB_TEST_COMMANDER_EMS_TOKEN", "a" * 40)
    monkeypatch.setenv("WIND_HUB_TEST_COMMANDER_MAINTENANCE_TOKEN", "b" * 40)
    assert runtime.get_rpc_metadata("commander", "SubmitCommand") == (
        ("authorization", "Bearer " + "a" * 40),
    )
    assert runtime.get_rpc_metadata("commander", "RecoverDeviceControl") == (
        ("authorization", "Bearer " + "b" * 40),
    )


def test_method_credentials_require_default_identity(tmp_path: Path) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  commander:
    grpc_endpoint: "127.0.0.1:50052"
    method_token_envs:
      RecoverDeviceControl: WIND_HUB_TEST_COMMANDER_MAINTENANCE_TOKEN
"""
    )
    with pytest.raises(ValueError, match="requires a default token_env"):
        load_config(_write(tmp_path, body))


@pytest.mark.parametrize(
    ("domain", "name", "version", "maintenance_env"),
    [
        (
            "e2e-final",
            "service_cli-e2e_v1.1.7.yaml",
            "1.1.7",
            "WIND_HUB_E2E_COMMANDER_MAINTENANCE_TOKEN",
        ),
        (
            "sim-a",
            "service_cli-sim_v1.10.0.yaml",
            "1.10.0",
            "WIND_HUB_SIM_COMMANDER_MAINTENANCE_TOKEN",
        ),
        ("prod-a", "service_cli_v1.4.0.yaml", "1.4.0", "WIND_HUB_COMMANDER_MAINTENANCE_TOKEN"),
    ],
)
def test_active_candidate_cli_transport(
    domain: str, name: str, version: str, maintenance_env: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    source_path = root / "configs" / "services" / name
    active_path = root / "deploy" / "config_roots" / domain / "services" / name
    assert source_path.read_bytes() == active_path.read_bytes()
    assert f'file_version: "{version}"' in active_path.read_text(encoding="utf-8")
    configured = load_config(active_path)
    assert configured.services["commander"].token_env is not None
    assert dict(configured.services["commander"].method_token_envs) == {
        "RecoverDeviceControl": maintenance_env
    }
    assert configured.services["gateway"].token_env is not None
    assert configured.services["data-generator"].token_env is not None
    assert configured.services["commander"].tls_profile is None
    expected = "local" if domain == "prod-a" else None
    assert configured.services["collector"].tls_profile == expected
    if expected is not None:
        assert expected in configured.profiles
    if domain == "e2e-final":
        for role in ("collector-realtime", "collector-reliable"):
            assert configured.services[role].tls_profile is None


@pytest.mark.parametrize("service", ["collector", "collector_realtime", "collector_reliable"])
def test_collector_management_credential_applies_to_deployed_target_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: str
) -> None:
    body = (
        _BASE_HEADER
        + f"""
services:
  {service}:
    grpc_endpoint: "127.0.0.1:50051"
"""
    )
    runtime = Runtime(_write(tmp_path, body), None)
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", "a" * 40)
    assert runtime.get_rpc_metadata(service, "StartTask") == (("x-windhub-admin-token", "a" * 40),)
    assert runtime.get_rpc_metadata(service, "GetServiceInfo") is None
