"""cli.yaml 独立加载与边界校验（设计 RR-01/CR-01～06）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.runtime import load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cli.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_BASE_HEADER = """
schema_version: 1
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
cli:
  timeout_seconds: 1
  timeout_seconds: 2
"""
    with pytest.raises(ValueError, match="duplicate YAML key"):
        load_config(_write(tmp_path, body))


def test_endpoint_and_rpc_via_are_mutually_exclusive(tmp_path: Path) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  collector:
    grpc_endpoint: "localhost:5000"
    rpc_via: gateway
    tls_profile: mtls
tls_profiles:
  mtls:
    ca_cert: ca.crt
    client_cert: client.crt
    client_key: client.key
"""
    )
    with pytest.raises(ValueError, match="together"):
        load_config(_write(tmp_path, body))


def test_endpoint_requires_tls_profile(tmp_path: Path) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  collector:
    grpc_endpoint: "localhost:5000"
"""
    )
    with pytest.raises(ValueError, match="tls_profile is required"):
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


def test_rpc_via_cycle_rejected(tmp_path: Path) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  a:
    rpc_via: b
  b:
    rpc_via: a
"""
    )
    with pytest.raises(ValueError, match="cyclic rpc_via"):
        load_config(_write(tmp_path, body))


def test_rpc_via_dangling_rejected(tmp_path: Path) -> None:
    body = (
        _BASE_HEADER
        + """
services:
  a:
    rpc_via: ghost
"""
    )
    with pytest.raises(ValueError, match="invalid or cyclic rpc_via"):
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
