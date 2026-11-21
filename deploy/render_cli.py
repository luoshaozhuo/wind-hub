"""Create a deterministic, explicit CLI service map for a prepared deployment.

This does not create containers, change certificates, or launch any business process.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml

SERVICE_NAMES = (
    "gateway",
    "data-generator",
    "collector_realtime",
    "collector_reliable",
    "sink",
    "commander",
)

DEFAULT_DEPLOYMENT_PATH = "configs/deployments/deployment_prod-a_v1.2.0.yaml"

DEFAULT_FILES = {
    "gateway": "service_gateway_v1.2.0.yaml",
    "data-generator": "service_data-generator_v1.1.0.yaml",
    "collector_realtime": "service_collector-realtime_v1.1.0.yaml",
    "collector_reliable": "service_collector-reliable_v1.0.0.yaml",
    "sink": "service_sink-west_v1.2.0.yaml",
    "commander": "service_commander_v1.4.0.yaml",
}

PORTS = {
    "gateway": 50073,
    "data-generator": 50061,
    "collector_realtime": 50051,
    "collector_reliable": 50054,
    "sink": 50053,
    "commander": 50052,
}

COLLECTOR_ENV_KEYS = frozenset(
    {
        "WIND_HUB_COLLECTOR_TLS_CERT",
        "WIND_HUB_COLLECTOR_TLS_KEY",
        "WIND_HUB_COLLECTOR_TLS_CA",
        "WIND_HUB_COLLECTOR_ADMIN_TOKEN",
    }
)


def _collector_environment(
    path: Path | None, *, required: bool, check_files: bool
) -> dict[str, str]:
    """读取 Collector 的正式 mTLS 与管理身份，绝不输出密钥或 Token 内容。"""
    if path is None:
        if required:
            raise ValueError("Collector mTLS requires --collector-env-file")
        return {}
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError("Collector env file must be an absolute regular file")
    if path.stat().st_mode & 0o077:
        raise ValueError("Collector env file must be private (0600)")
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in COLLECTOR_ENV_KEYS or key in result or not value:
            raise ValueError("Collector env file has invalid or duplicate entries")
        result[key] = value
    if set(result) != COLLECTOR_ENV_KEYS:
        raise ValueError("Collector env file requires TLS cert, key, CA and admin token")
    for key in (
        "WIND_HUB_COLLECTOR_TLS_CERT",
        "WIND_HUB_COLLECTOR_TLS_KEY",
        "WIND_HUB_COLLECTOR_TLS_CA",
    ):
        credential = Path(result[key])
        if not credential.is_absolute():
            raise ValueError(f"{key} must be an absolute path")
        if check_files and (not credential.is_file() or credential.is_symlink()):
            raise ValueError(f"{key} must reference a regular non-symlinked file")
        if (
            check_files
            and key == "WIND_HUB_COLLECTOR_TLS_KEY"
            and credential.stat().st_mode & 0o077
        ):
            raise ValueError("Collector TLS private key must not be group/other accessible")
    return result


def _document(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing or symlinked service YAML: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] not in (1, 2)
    ):
        raise ValueError(f"invalid service schema: {path}")
    return data


def _relative(root: Path, raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"deployment config must be root-relative: {raw}")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"deployment config escapes root: {raw}")
    return resolved


def _service_files(root: Path, names: dict[str, str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for name in SERVICE_NAMES:
        filename = names[name]
        if "/" in filename or filename in {".", ".."}:
            raise ValueError(f"{name}: expected a filename in configs/services")
        path = _relative(root, f"configs/services/{filename}")
        _document(path)
        result[name] = path
    return result


def _section(document: dict[str, Any], key: str, service: str) -> dict[str, Any]:
    value = document.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{service}: {key} must be a mapping")
    return value


def _validate_bindings(root: Path, files: dict[str, Path], mode: str) -> None:
    docs = {name: _document(path) for name, path in files.items()}
    for name in ("collector_realtime", "collector_reliable"):
        expected = "realtime" if name == "collector_realtime" else "reliable"
        delivery = _section(docs[name], "messaging", name).get("delivery")
        if delivery != expected:
            raise ValueError(f"{name}: expected messaging.delivery={expected}")
    for name in ("gateway", "data-generator", "commander", "sink"):
        doc = docs[name]
        if name == "data-generator":
            raw = _section(doc, "service", name).get("grpc_bind", "")
            if not isinstance(raw, str):
                raise ValueError(f"{name}: service.grpc_bind must be a string")
            actual = int(raw.rsplit(":", 1)[1]) if re.search(r":[0-9]+$", raw) else None
        else:
            actual = _section(doc, "grpc", name).get("listen_port")
        if name == "sink":
            expected_root = str(root) if mode == "process" else "/etc/wind-hub"
            if doc.get("config_root") != expected_root:
                raise ValueError(f"sink: config_root must be {expected_root}")
        if actual != PORTS[name]:
            raise ValueError(f"{name}: gRPC listen port {actual} differs from {PORTS[name]}")
    # Resolve Service/Instance versions from one published Deployment selection.
    deployment_path = _relative(root, DEFAULT_DEPLOYMENT_PATH)
    deployment = _document(deployment_path)
    selected_services = _section(deployment, "services", "Deployment")
    selected_instances = _section(deployment, "instances", "Deployment")
    role_types = {
        "site": "site",
        "devices": "device",
        "tasks": "collector_tasks",
        "sinks": "sink",
        "deliveries": "delivery",
        "scenarios": "scenario",
    }
    service_ids = {
        "collector_realtime": "collector-realtime",
        "collector_reliable": "collector-reliable",
        "sink": "sink-west",
    }
    for name, doc in docs.items():
        service_id = service_ids.get(name, name)
        if doc.get("service_id") != service_id:
            raise ValueError(f"{name}: Service identity does not match deployment")
        selected = _section(selected_services, service_id, "Deployment")
        if selected.get("config_id") != service_id:
            raise ValueError(f"{name}: missing Deployment Service selection")
        if selected.get("file_version") != doc.get("file_version"):
            raise ValueError(f"{name}: selected Service version differs from file")
        sources = doc.get("config_sources", {})
        if not isinstance(sources, dict) or set(sources) != {"instances"}:
            raise ValueError(f"{name}: Service may only reference Instance identities")
        instances = _section(sources, "instances", name)
        for role, config_id in instances.items():
            config_type = role_types.get(role)
            if config_type is None or not isinstance(config_id, str) or not config_id:
                raise ValueError(f"{name}: unsupported Instance identity {role}")
            selected_type = _section(selected_instances, config_type, "Deployment")
            version = _section(selected_type, config_id, "Deployment").get("file_version")
            if not isinstance(version, str):
                raise ValueError(f"{name}: required Instance version is missing")
            prefix = {
                "site": "site", "device": "device",
                "collector_tasks": "collector_tasks", "scenario": "scenarios",
                "sink": "sinks", "delivery": "deliveries",
            }[config_type]
            relative = f"configs/instances/{prefix}_{config_id}_v{version}.yaml"
            if not _relative(root, relative).is_file():
                raise ValueError(f"{name}: required Instance file is missing: {relative}")


def _argv(name: str, paths: dict[str, Path], root: Path, python: Path) -> list[str]:
    base = [str(python), "-m"]
    if name == "gateway":
        return [
            *base,
            "industrial_gateway.main",
            "--config",
            str(paths[name]),
            "--project-root",
            str(root),
        ]
    if name == "data-generator":
        return [*base, "data_generator.main", "--config", str(paths[name])]
    if name.startswith("collector_"):
        return [
            *base,
            "collector.main",
            "--config",
            str(paths[name]),
            "--state-directory",
            str(root / "state" / name),
            "--collector-id",
            name,
            "--grpc-host",
            "127.0.0.1",
            "--grpc-port",
            str(PORTS[name]),
        ]
    if name == "sink":
        return [*base, "sink_service.main", "--config-root", str(root)]
    return [*base, "commander", "--config", str(paths[name])]


def _render(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.resolve(strict=True)
    paths = _service_files(
        root, {name: getattr(args, name.replace("-", "_")) for name in SERVICE_NAMES}
    )
    _validate_bindings(root, paths, args.mode)
    certs = (args.ca, args.client_cert, args.client_key)
    for cert in certs:
        if not cert.is_file() or cert.is_symlink():
            raise ValueError(f"missing or symlinked TLS credential: {cert}")
    if args.client_key.stat().st_mode & 0o077:
        raise ValueError("TLS client key must not be accessible by group or other users")

    if args.mode == "containerd" and (not args.socket.is_absolute() or not args.namespace):
        raise ValueError("containerd requires an absolute socket and a namespace")
    if args.mode == "isulad" and not args.isula.is_absolute():
        raise ValueError("isulad executable must be an absolute path")
    if args.mode == "process":
        python = args.python.resolve(strict=True)
        if not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError(f"invalid Python executable: {python}")
        collector_env = _collector_environment(
            args.collector_env_file, required=True, check_files=True
        )
        collector_env.pop("WIND_HUB_COLLECTOR_ADMIN_TOKEN")
    else:
        if args.collector_env_file is not None:
            raise ValueError("--collector-env-file is supported only for process mode")
        collector_env = {}

    if args.runtime_prefix is not None and (
        args.mode != "rpc" or not args.runtime_prefix.is_absolute()
    ):
        raise ValueError("--runtime-prefix is supported only for RPC-only CLI mode")

    def client_path(path: Path) -> str:
        source = path.resolve(strict=True)
        if args.runtime_prefix is None:
            return str(source)
        if not source.is_relative_to(root):
            raise ValueError(f"TLS material must be under root for container path mapping: {path}")
        return str(args.runtime_prefix / source.relative_to(root))

    services: dict[str, Any] = {}
    for name in SERVICE_NAMES:
        row: dict[str, Any] = {
            "grpc_endpoint": f"127.0.0.1:{PORTS[name]}",
        }
        if name != "sink":
            row["tls_profile"] = "local"
        if args.mode == "process":
            row.update(
                {
                    "manager": "process",
                    "target": name,
                    "process": {
                        "argv": _argv(name, paths, root, python),
                        "cwd": str(root),
                        "env": collector_env if name.startswith("collector_") else {},
                        "state_dir": str(root / "state" / "cli-processes"),
                        "log_file": str(root / "logs" / f"{name}.log"),
                    },
                }
            )
        elif args.mode == "containerd":
            row.update(
                {
                    "manager": "containerd",
                    "target": f"wind-hub-{name.replace('_', '-')}",
                    "namespace": args.namespace,
                    "manager_socket": str(args.socket),
                }
            )
        elif args.mode == "isulad":
            row.update(
                {
                    "manager": "isulad",
                    "target": f"wind-hub-{name.replace('_', '-')}",
                    "isula_binary": str(args.isula),
                }
            )
        services[name] = row
    services["collector"] = {
        "grpc_endpoint": services["collector_realtime"]["grpc_endpoint"],
        "tls_profile": "local",
    }
    return {
        "schema_version": 1,
        "file_version": "1.1.0",
        "cli": {"timeout_seconds": 30, "output_format": "json"},
        "services": services,
        "tls_profiles": {
            "local": {
                "ca_cert": client_path(args.ca),
                "client_cert": client_path(args.client_cert),
                "client_key": client_path(args.client_key),
            }
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("process", "containerd", "isulad", "rpc"), required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--namespace", default="wind-hub")
    parser.add_argument("--socket", type=Path, default=Path("/run/containerd/containerd.sock"))
    parser.add_argument(
        "--isula", type=Path, default=Path(shutil.which("isula") or "/usr/bin/isula")
    )
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument("--client-cert", type=Path, required=True)
    parser.add_argument("--client-key", type=Path, required=True)
    parser.add_argument("--collector-env-file", type=Path)
    parser.add_argument("--runtime-prefix", type=Path, default=None)
    for name, default in DEFAULT_FILES.items():
        parser.add_argument(f"--{name.replace('_', '-')}", default=default)
    args = parser.parse_args(argv)
    try:
        payload = _render(args)
        output = args.output.absolute()
        if output.exists():
            raise ValueError(f"refusing to overwrite existing CLI configuration: {output}")
        if not output.parent.is_dir():
            raise ValueError(f"CLI config parent directory does not exist: {output.parent}")
        fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump(payload, stream, allow_unicode=True, sort_keys=False)
        print(f"CLI deployment config: {output}")
        return 0
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.exit(2, f"deployment config error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
