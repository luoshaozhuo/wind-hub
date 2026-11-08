"""CLI-owned configuration, shared grpc.aio channels, and stable output."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import yaml
from google.protobuf.json_format import MessageToDict
from rich.console import Console
from rich.table import Table


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys before interpreting service endpoints."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in seen:
                raise ValueError(f"non-string or duplicate YAML key: {key!r}")
            seen.add(key)
        mapping: dict[Any, Any] = super().construct_mapping(node, deep=deep)
        return mapping


@dataclass(frozen=True, slots=True)
class ServiceTarget:
    service_id: str
    grpc_endpoint: str | None
    rpc_via: str | None
    tls_profile: str | None
    manager: str | None
    target: str | None
    namespace: str | None
    manager_socket: str | None
    isula_binary: str | None
    systemd_scope: str | None
    timeout: float


@dataclass(frozen=True, slots=True)
class TLSProfile:
    ca_cert: Path
    client_cert: Path
    client_key: Path


@dataclass(frozen=True, slots=True)
class CLIConfig:
    services: dict[str, ServiceTarget]
    profiles: dict[str, TLSProfile]
    timeout: float
    output_format: str
    max_hosts: int
    max_concurrency: int
    probe_timeout: float
    industrial_ports: tuple[int, ...]
    prompt: str
    history: bool
    max_display_rows: int
    poll_interval_ms: int
    poll_reconnect_attempts: int
    snapshot_max_bytes: int
    diff_max_file_bytes: int
    diff_max_output_lines: int


def _range(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return number


def _int_range(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _path(config_path: Path, raw: Any, name: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{name} requires a path")
    return (config_path.parent / raw).resolve()


def load_config(path: Path) -> CLIConfig:
    """Validate the separate cli.yaml without loading service business YAML."""
    if not path.is_file():
        raise ValueError(f"CLI configuration not found: {path}")
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid CLI YAML: {exc}") from exc
    if (
        not isinstance(raw, dict)
        or type(raw.get("schema_version")) is not int
        or raw["schema_version"] != 1
    ):
        raise ValueError("CLI schema_version must be 1")
    cli = raw.get("cli", {})
    probes = raw.get("network_probe", {})
    services = raw.get("services", {})
    tls = raw.get("tls_profiles", {})
    if not all(isinstance(obj, dict) for obj in (cli, probes, services, tls)):
        raise ValueError("cli/services/tls_profiles/network_probe must be mappings")
    timeout = _range(cli.get("timeout_seconds", 5), "cli.timeout_seconds", 1, 120)
    output_format = cli.get("output_format", "table")
    if output_format not in ("table", "json"):
        raise ValueError("cli.output_format must be table or json")
    prompt = cli.get("prompt", "wind-hub> ")
    history = cli.get("history", True)
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("cli.prompt must be a non-empty string")
    if type(history) is not bool:
        raise ValueError("cli.history must be boolean")
    max_display_rows = _int_range(cli.get("max_display_rows", 1000), "max_display_rows", 1, 10000)
    poll_interval_ms = _int_range(cli.get("poll_interval_ms", 1000), "poll_interval_ms", 100, 60000)
    poll_reconnect_attempts = _int_range(
        cli.get("poll_reconnect_attempts", 3), "poll_reconnect_attempts", 0, 10
    )
    snapshot_max_bytes = _int_range(
        cli.get("snapshot_max_bytes", 67108864), "snapshot_max_bytes", 1, 67108864
    )
    diff_max_file_bytes = _int_range(
        cli.get("diff_max_file_bytes", 1048576), "diff_max_file_bytes", 1, 1048576
    )
    diff_max_output_lines = _int_range(
        cli.get("diff_max_output_lines", 10000), "diff_max_output_lines", 1, 10000
    )
    profiles: dict[str, TLSProfile] = {}
    for name, item in tls.items():
        if not isinstance(item, dict):
            raise ValueError(f"invalid TLS profile {name}")
        profiles[name] = TLSProfile(
            _path(path, item.get("ca_cert"), "ca_cert"),
            _path(path, item.get("client_cert"), "client_cert"),
            _path(path, item.get("client_key"), "client_key"),
        )
    targets: dict[str, ServiceTarget] = {}
    for name, item in services.items():
        if not isinstance(name, str) or not name or not isinstance(item, dict):
            raise ValueError("service definition must be a named mapping")
        endpoint, rpc_via = item.get("grpc_endpoint"), item.get("rpc_via")
        if endpoint is not None and (not isinstance(endpoint, str) or not endpoint):
            raise ValueError(f"{name}.grpc_endpoint must be non-empty")
        if rpc_via is not None and (not isinstance(rpc_via, str) or not rpc_via):
            raise ValueError(f"{name}.rpc_via must be non-empty")
        if endpoint is not None and rpc_via is not None:
            raise ValueError(f"{name} cannot define grpc_endpoint and rpc_via together")
        profile = item.get("tls_profile")
        if profile is not None and profile not in profiles:
            raise ValueError(f"{name} references unknown tls_profile {profile}")
        manager = item.get("manager")
        if manager is not None and manager not in ("containerd", "isulad", "systemd"):
            raise ValueError(f"{name}.manager is unsupported")
        target = item.get("target")
        if manager is not None and (not isinstance(target, str) or not target):
            raise ValueError(f"{name}.target is required for manager")
        namespace = item.get("namespace")
        manager_socket = item.get("manager_socket")
        isula_binary = item.get("isula_binary")
        systemd_scope = item.get("systemd_scope")
        if manager == "containerd":
            if not isinstance(namespace, str) or not namespace:
                raise ValueError(f"{name}.namespace is required")
            if not isinstance(manager_socket, str) or not Path(manager_socket).is_absolute():
                raise ValueError(f"{name}.manager_socket must be absolute")
        if (
            manager == "isulad"
            and isula_binary is not None
            and (not isinstance(isula_binary, str) or not Path(isula_binary).is_absolute())
        ):
            raise ValueError(f"{name}.isula_binary must be absolute")
        if systemd_scope is not None and systemd_scope not in ("system", "user"):
            raise ValueError(f"{name}.systemd_scope must be system or user")
        if endpoint is not None and profile is None:
            raise ValueError(f"{name}.tls_profile is required for mTLS")
        targets[name] = ServiceTarget(
            name,
            endpoint,
            rpc_via,
            profile,
            manager,
            target,
            namespace,
            manager_socket,
            isula_binary,
            systemd_scope,
            timeout,
        )
    for name, target in targets.items():
        visited = {name}
        while target.rpc_via is not None:
            alias = target.rpc_via
            if alias not in targets or alias in visited:
                raise ValueError(f"invalid or cyclic rpc_via for {name}")
            visited.add(alias)
            target = targets[alias]
        if target.grpc_endpoint is None and (
            targets[name].rpc_via is not None or targets[name].manager is None
        ):
            raise ValueError(f"{name} has no valid gRPC target or process manager")
    raw_ports = probes.get("industrial_tcp_ports", [102, 502, 2404, 4840, 44818, 48898])
    if (
        not isinstance(raw_ports, list)
        or not raw_ports
        or len(raw_ports) > 64
        or any(type(port) is not int or not 1 <= port <= 65535 for port in raw_ports)
        or len(set(raw_ports)) != len(raw_ports)
    ):
        raise ValueError("network_probe.industrial_tcp_ports must contain unique valid ports")
    return CLIConfig(
        targets,
        profiles,
        timeout,
        output_format,
        _int_range(probes.get("max_hosts", 1024), "max_hosts", 1, 4096),
        _int_range(probes.get("max_concurrency", 64), "max_concurrency", 1, 256),
        _range(probes.get("timeout_seconds", 1), "probe timeout", 0.1, 30),
        tuple(raw_ports),
        prompt,
        history,
        max_display_rows,
        poll_interval_ms,
        poll_reconnect_attempts,
        snapshot_max_bytes,
        diff_max_file_bytes,
        diff_max_output_lines,
    )


class Runtime:
    """All grpc.aio channels are created and closed on the invoking event loop."""

    def __init__(self, config_path: Path | None, output_format: str | None) -> None:
        self.config_path = config_path
        self.output_format = output_format
        self._config: CLIConfig | None = None
        self._channels: dict[str, grpc.aio.Channel] = {}
        self.console = Console()

    def config(self) -> CLIConfig:
        if self._config is None:
            if self.config_path is None:
                raise ValueError("remote command requires --config CLI_YAML")
            self._config = load_config(self.config_path)
        return self._config

    def target(self, name: str) -> ServiceTarget:
        aliases = {"sink": "sink_realtime", "data-generator": "data_generator"}
        targets = self.config().services
        resolved = name if name in targets else aliases.get(name, name)
        try:
            return targets[resolved]
        except KeyError as exc:
            raise ValueError(f"service not configured: {name}") from exc

    def _resolve(self, name: str) -> ServiceTarget:
        target = self.target(name)
        while target.rpc_via is not None:
            target = self.target(target.rpc_via)
        return target

    def channel(self, name: str) -> grpc.aio.Channel:
        target = self._resolve(name)
        if target.service_id not in self._channels:
            endpoint = target.grpc_endpoint
            if endpoint is None:
                raise ValueError(f"service has no gRPC endpoint: {name}")
            if target.tls_profile is None:
                raise ValueError(f"mTLS profile is required for {name}")
            profile = self.config().profiles[target.tls_profile]
            credentials = grpc.ssl_channel_credentials(
                root_certificates=profile.ca_cert.read_bytes(),
                private_key=profile.client_key.read_bytes(),
                certificate_chain=profile.client_cert.read_bytes(),
            )
            max_message = self.config().snapshot_max_bytes * 2 + 1048576
            channel = grpc.aio.secure_channel(
                endpoint,
                credentials,
                options=(
                    ("grpc.max_send_message_length", max_message),
                    ("grpc.max_receive_message_length", max_message),
                ),
            )
            self._channels[target.service_id] = channel
        return self._channels[target.service_id]

    async def call_common(self, name: str, method: str) -> dict[str, Any]:
        target = self.target(name)
        from contracts import common_pb2, common_pb2_grpc

        stub = common_pb2_grpc.CommonServiceStub(self.channel(name))
        result = await getattr(stub, method)(common_pb2.Empty(), timeout=target.timeout)
        return MessageToDict(
            result,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )

    async def close(self) -> None:
        await asyncio.gather(*(channel.close() for channel in self._channels.values()))
        self._channels.clear()

    def present(self, command: str, data: Any) -> int:
        status, exit_code = self._result_state(command, data)
        target = None
        if isinstance(data, dict):
            if isinstance(data.get("host"), str) and "port" in data:
                target = f"{data['host']}:{data['port']}"
            else:
                target = next(
                    (
                        data[key]
                        for key in ("device_id", "gateway_id", "generation_id", "target")
                        if isinstance(data.get(key), str)
                    ),
                    None,
                )
        payload = {
            "schema_version": 1,
            "command": command,
            "status": status,
            "target": target,
            "data": data,
            "error_code": None,
        }
        fmt = self.output_format or (self._config.output_format if self._config else "table")
        if fmt == "json":
            print(json.dumps(payload, ensure_ascii=False, default=str))
            return exit_code
        if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
            keys = list(dict.fromkeys(key for item in data for key in item))
            table = Table(title=command)
            for key in keys:
                table.add_column(key)
            limit = self._config.max_display_rows if self._config else 1000
            for item in data[:limit]:
                table.add_row(*(str(item.get(key, "")) for key in keys))
            self.console.print(table)
            if len(data) > limit:
                self.console.print(
                    f"Displayed {limit} of {len(data)} rows; use JSON for complete output."
                )
        else:
            self.console.print(data)
        return exit_code

    @staticmethod
    def _result_state(command: str, data: Any) -> tuple[str, int]:
        if command == "service status" and isinstance(data, list):
            degraded = 0
            for item in data:
                if not isinstance(item, dict):
                    degraded += 1
                    continue
                process = item.get("process", {})
                business = item.get("business", {})
                if isinstance(process, dict):
                    # systemd 用 active_state/status；containerd/iSulad 用 state。
                    manager_state = process.get("state") or process.get("active_state")
                    if process.get("status") == "UNKNOWN" or manager_state in {
                        "STOPPED",
                        "UNKNOWN",
                        "failed",
                        "inactive",
                    }:
                        degraded += 1
                        continue
                if isinstance(business, dict) and business.get(
                    "health", business.get("status")
                ) in ("UNKNOWN", "UNAVAILABLE", "FAILED"):
                    degraded += 1
            if degraded == len(data) and degraded > 0:
                return "UNKNOWN", 3
            if degraded:
                return "PARTIAL", 4
        if not isinstance(data, dict):
            return "SUCCESS", 0
        if command == "config diff" and data.get("complete") is False:
            return "INCOMPLETE", 4
        if command in ("service start", "service stop", "service restart"):
            process = data.get("process")
            if not isinstance(process, dict):
                return "UNKNOWN", 3
            if process.get("returncode", 0) != 0:
                return "FAILURE", 1
            if process.get("confirmed") is False:
                return "UNKNOWN", 3
            stages = process.get("stages")
            if isinstance(stages, list) and any(
                isinstance(stage, dict) and stage.get("confirmed") is not True for stage in stages
            ):
                return "UNKNOWN", 3
            if command != "service stop":
                business = data.get("business")
                if not isinstance(business, dict):
                    return "UNKNOWN", 3
                if business.get("health") not in {"READY", "HEALTHY", "PASS"}:
                    return "UNKNOWN", 3
        if command == "net tcp" and data.get("tcp_state") != "OPEN":
            return "UNKNOWN", 3
        if command == "net ping" and data.get("status") != "UP":
            return "UNKNOWN", 3
        if command == "diagnose pipeline":
            overall = data.get("overall_status")
            if overall in ("FAIL", "FAILURE"):
                return "FAILURE", 1
            if overall not in ("PASS", "SUCCESS"):
                return "UNKNOWN", 3
        if data.get("accepted") is False or data.get("valid") is False:
            return "FAILURE", 1
        if command.startswith("commander ") and (
            data.get("state") == "UNKNOWN" or data.get("outcome") == "UNKNOWN"
        ):
            return "UNKNOWN", 3
        if command.endswith("ApplyConfigSnapshot") and data.get("restart_required") is True:
            return "PARTIAL", 4
        if command == "gateway update":
            results = data.get("results")
            if not isinstance(results, list) or not results:
                return "UNKNOWN", 3
            failed = [
                item
                for item in results
                if not isinstance(item, dict) or item.get("success") is not True
            ]
            if len(failed) == len(results):
                return "FAILURE", 1
            if failed:
                return "PARTIAL", 4
        # apply_results 仅在本次 ApplyConfigSnapshot 响应中决定退出码；config get 的
        # runtime 视图携带的是历史生效状态，只读查询成功必须是 0（设计 9.2.2/表 10-2）。
        results = data.get("apply_results")
        if (
            command.endswith("ApplyConfigSnapshot")
            and isinstance(results, list)
            and any(
                isinstance(item, dict) and item.get("status") not in ("SUCCESS", "APPLIED")
                for item in results
            )
        ):
            return "PARTIAL", 4
        return "SUCCESS", 0

    def interrupted(self, command: str) -> None:
        """表 6-2：用户中断 = INTERRUPTED（单次命令退出码 130 由调用方给出）。"""
        payload = {
            "schema_version": 1,
            "command": command,
            "status": "INTERRUPTED",
            "target": None,
            "data": None,
            "error_code": "INTERRUPTED",
            "message": "operation cancelled before completion; result UNKNOWN",
        }
        fmt = self.output_format or (self._config.output_format if self._config else "table")
        if fmt == "json":
            print(json.dumps(payload, ensure_ascii=False))
        else:
            self.console.print("[yellow]INTERRUPTED[/yellow]")

    def error(self, command: str, exc: Exception) -> None:
        if isinstance(exc, grpc.aio.AioRpcError):
            code = exc.code().name
            message = exc.details() or code
            if command == "commander write" and exc.code() in {
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
            }:
                message += "; write outcome UNKNOWN; do not resubmit blindly"
        else:
            code, message = type(exc).__name__, str(exc)
        unknown = (
            isinstance(exc, grpc.aio.AioRpcError)
            and exc.code()
            in {
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.DEADLINE_EXCEEDED,
                grpc.StatusCode.UNIMPLEMENTED,
                grpc.StatusCode.CANCELLED,
            }
        ) or isinstance(exc, TimeoutError)
        payload = {
            "schema_version": 1,
            "command": command,
            "status": "UNKNOWN" if unknown else "FAILURE",
            "target": None,
            "data": None,
            "error_code": code,
            "message": message,
        }
        fmt = self.output_format or (self._config.output_format if self._config else "table")
        if fmt == "json":
            print(json.dumps(payload, ensure_ascii=False))
        else:
            self.console.print(f"[red]{code}[/red]: {message}")
