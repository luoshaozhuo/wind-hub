"""Create a deterministic bearer-authenticated CLI service map without TLS material."""

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
    "configuration",
    "storage",
    "gateway",
    "data-generator",
    "collector_realtime",
    "collector_reliable",
    "sink",
    "commander",
)

DEFAULT_FILES = {
    "configuration": "service_configuration_v1.0.0.yaml",
    "storage": "storage_bootstrap_v1.0.0.yaml",
    "gateway": "industrial_gateway-sim.example.yaml",
    "data-generator": "data-generator-sim.example.yaml",
    "collector_realtime": "collector-realtime.yaml",
    "collector_reliable": "collector-reliable.yaml",
    "sink": "sink_service.yaml",
    "commander": "commander.yaml",
}

PORTS = {
    "configuration": 51060,
    "storage": 51110,
    "gateway": 50073,
    "data-generator": 50061,
    "collector_realtime": 50051,
    "collector_reliable": 50054,
    "sink": 50053,
    "commander": 50052,
}

def _document(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing or symlinked service YAML: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
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
    for name in ("configuration", "gateway", "data-generator", "commander", "sink"):
        doc = docs[name]
        if name == "configuration":
            actual = _section(doc, "grpc", name).get("port")
        elif name == "data-generator":
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
    for name, doc in docs.items():
        if name in {"configuration", "storage"}:
            continue
        sources = doc.get("config_sources", {})
        if not isinstance(sources, dict):
            raise ValueError(f"{name}: config_sources must be a mapping")
        for group in ("instances", "templates"):
            entries = sources.get(group)
            if not isinstance(entries, dict) or not entries:
                raise ValueError(f"{name}: config_sources.{group} must be a non-empty mapping")
            for label, value in entries.items():
                if not isinstance(value, str):
                    raise ValueError(f"{name}: invalid config source {label}")
                path = _relative(root, value)
                if not path.exists():
                    raise ValueError(f"{name}: missing config source {path}")


def _argv(name: str, paths: dict[str, Path], root: Path, python: Path) -> list[str]:
    base = [str(python), "-m"]
    if name == "configuration":
        return [*base, "configuration_service", "--config", str(paths[name])]
    if name == "storage":
        return [*base, "storage_service.main", "--bootstrap", str(paths[name])]
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
    if args.mode == "containerd" and (not args.socket.is_absolute() or not args.namespace):
        raise ValueError("containerd requires an absolute socket and a namespace")
    if args.mode == "isulad" and not args.isula.is_absolute():
        raise ValueError("isulad executable must be an absolute path")
    if args.mode == "process":
        python = args.python.resolve(strict=True)
        if not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError(f"invalid Python executable: {python}")
    services: dict[str, Any] = {}
    for name in SERVICE_NAMES:
        row: dict[str, Any] = {
            "grpc_endpoint": f"127.0.0.1:{PORTS[name]}",
        }
        if args.mode == "process":
            row.update(
                {
                    "manager": "process",
                    "target": name,
                    "process": {
                        "argv": _argv(name, paths, root, python),
                        "cwd": str(root),
                        "env": {},
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
    }
    return {
        "schema_version": 1,
        "file_version": "1.0.0",
        "cli": {"timeout_seconds": 30, "output_format": "json"},
        "services": services,

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
