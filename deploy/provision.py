"""Prepare explicit OCI containers for CLI-managed lifecycle on an isolated Linux host.

Container creation is a deployment operation, not a business lifecycle command.
The default is a non-mutating command preview; --apply performs actual creation.
"""

from __future__ import annotations

import argparse
import os
import shlex
import stat
import subprocess
from pathlib import Path

SERVICE_FILES = {
    "gateway": "industrial_gateway-sim.example.yaml",
    "data-generator": "data-generator-sim.example.yaml",
    "collector_realtime": "collector-realtime.yaml",
    "collector_reliable": "collector-reliable.yaml",
    "sink": "sink_service.yaml",
    "commander": "commander.yaml",
}
PORTS = {"collector_realtime": 50051, "collector_reliable": 50054}


def _commands(name: str) -> list[str]:
    service = f"/etc/wind-hub/configs/services/{SERVICE_FILES[name]}"
    if name == "gateway":
        return [
            "industrial_gateway.main", "--config", service,
            "--project-root", "/etc/wind-hub",
        ]
    if name == "data-generator":
        return ["data_generator.main", "--config", service]
    if name.startswith("collector_"):
        return [
            "collector.main", "--config", service,
            "--state-directory", f"/etc/wind-hub/state/{name}",
            "--collector-id", name,
            "--grpc-host", "127.0.0.1",
            "--grpc-port", str(PORTS[name]),
        ]
    if name == "sink":
        return ["sink_service.main", "--config-root", "/etc/wind-hub"]
    return ["commander", "--config", service]


def _private_env(path: Path | None) -> list[str]:
    if path is None:
        return []
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError("env file must be an absolute regular file")
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o077:
        raise ValueError("env file must be private (0600)")
    return ["--env-file", str(path)]


def _build(args: argparse.Namespace) -> list[list[str]]:
    root = args.root.resolve(strict=True)
    if not root.is_dir() or not args.image or args.image.startswith("-"):
        raise ValueError("invalid deployment root or OCI image name")
    if args.uid < 1 or args.gid < 1:
        raise ValueError("run the deployment service under a non-root UID/GID")
    if args.runtime == "containerd" and (
        not args.socket.is_absolute() or not args.namespace
    ):
        raise ValueError("containerd requires an absolute socket and namespace")
    if args.runtime == "isulad" and not args.isula.is_absolute():
        raise ValueError("isulad executable path must be absolute")
    env_flags = _private_env(args.collector_env_file)
    if args.apply and args.collector_env_file is None and args.service in {
        "all", "collector_realtime", "collector_reliable"
    }:
        raise ValueError("Collector provisioning requires --collector-env-file")
    result: list[list[str]] = []
    names = SERVICE_FILES if args.service == "all" else (args.service,)
    for name in names:
        path = root / "configs" / "services" / SERVICE_FILES[name]
        if not path.is_file():
            raise ValueError(f"missing service file: {path}")
        ident = f"wind-hub-{name.replace('_', '-')}"
        role_env = env_flags if name.startswith("collector_") else []
        if args.runtime == "containerd":
            base = [
                args.ctr,
                "--address", str(args.socket),
                "--namespace", args.namespace,
            ]
            # ctr uses OCI process args, replacing the Docker image Entrypoint.
            result.append(
                [
                    *base, "containers", "create",
                    "--net-host", "--user", f"{args.uid}:{args.gid}",
                    "--mount",
                    f"type=bind,src={root},dst=/etc/wind-hub,options=rbind:rw",
                    *role_env,
                    args.image, ident,
                    "python", "-m", *_commands(name),
                ]
            )
            # ctr creates/starts the initial task; subsequent restart and
            # stopped-task recreation are managed by the production CLI.
            result.append([*base, "tasks", "start", "--null-io", "--detach", ident])
        else:
            result.append(
                [
                    str(args.isula), "create",
                    "--name", ident,
                    "--network", "host",
                    "--user", f"{args.uid}:{args.gid}",
                    "-v", f"{root}:/etc/wind-hub:rw",
                    *role_env,
                    args.image,
                    *_commands(name),
                ]
            )
            # iSulad initial create leaves the container stopped: use
            # 'wind-hub service start <name>' for lifecycle management.
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("containerd", "isulad"), required=True)
    parser.add_argument("--service", choices=("all", *SERVICE_FILES), default="all")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", default="wind-hub:local")
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("--collector-env-file", type=Path)
    parser.add_argument("--ctr", default="ctr")
    parser.add_argument("--socket", type=Path, default=Path("/run/containerd/containerd.sock"))
    parser.add_argument("--namespace", default="wind-hub")
    parser.add_argument("--isula", type=Path, default=Path("/usr/bin/isula"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        commands = _build(args)
        for command in commands:
            # Never print environment file contents.
            print(shlex.join(command), flush=True)
            if args.apply:
                subprocess.run(command, check=True, timeout=120)
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        parser.exit(2, f"OCI provision failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
