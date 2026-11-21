"""Prepare explicit OCI containers for CLI-managed lifecycle on an isolated Linux host.

Container creation is a deployment operation, not a business lifecycle command.
The default is a non-mutating command preview; --apply performs actual creation.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path

from render_cli import (
    DEFAULT_DEPLOYMENT_PATH,
    SERVICE_NAMES,
    _collector_environment,
    _relative,
    _selected_service_names,
    _service_files,
    _validate_bindings,
)

PORTS = {"collector_realtime": 50051, "collector_reliable": 50054}


def _commands(name: str, service_files: dict[str, str]) -> list[str]:
    service = f"/etc/wind-hub/configs/services/{service_files[name]}"
    if name == "gateway":
        return [
            "industrial_gateway.main",
            "--config",
            service,
            "--project-root",
            "/etc/wind-hub",
        ]
    if name == "data-generator":
        return ["data_generator.main", "--config", service]
    if name.startswith("collector_"):
        return [
            "collector.main",
            "--config",
            service,
            "--state-directory",
            f"/etc/wind-hub/state/{name}",
            "--collector-id",
            name,
            "--grpc-host",
            "127.0.0.1",
            "--grpc-port",
            str(PORTS[name]),
        ]
    if name == "sink":
        return ["sink_service.main", "--config-root", "/etc/wind-hub"]
    return ["commander", "--config", service]


def _private_env(path: Path | None, *, required: bool) -> list[str]:
    environment = _collector_environment(path, required=required, check_files=False)
    return ["--env-file", str(path)] if environment else []


def _build(args: argparse.Namespace) -> list[list[str]]:
    root = args.root.resolve(strict=True)
    if not root.is_dir() or not args.image or args.image.startswith("-"):
        raise ValueError("invalid deployment root or OCI image name")
    if args.uid < 1 or args.gid < 1:
        raise ValueError("run the deployment service under a non-root UID/GID")
    if args.runtime == "containerd" and (not args.socket.is_absolute() or not args.namespace):
        raise ValueError("containerd requires an absolute socket and namespace")
    if args.runtime == "isulad" and not args.isula.is_absolute():
        raise ValueError("isulad executable path must be absolute")
    deployment_path = _relative(root, args.deployment)
    service_files = _selected_service_names(root, deployment_path)
    files = _service_files(root, service_files)
    _validate_bindings(root, files, "container", deployment_path)
    env_flags = _private_env(
        args.collector_env_file,
        required=args.apply and args.service in {"all", "collector_realtime", "collector_reliable"},
    )
    result: list[list[str]] = []
    names = SERVICE_NAMES if args.service == "all" else (args.service,)
    for name in names:
        path = root / "configs" / "services" / service_files[name]
        if not path.is_file():
            raise ValueError(f"missing service file: {path}")
        ident = f"wind-hub-{name.replace('_', '-')}"
        role_env = env_flags if name.startswith("collector_") else []
        if args.runtime == "containerd":
            base = [
                args.ctr,
                "--address",
                str(args.socket),
                "--namespace",
                args.namespace,
            ]
            # ctr uses OCI process args, replacing the Docker image Entrypoint.
            result.append(
                [
                    *base,
                    "containers",
                    "create",
                    "--net-host",
                    "--user",
                    f"{args.uid}:{args.gid}",
                    "--mount",
                    f"type=bind,src={root},dst=/etc/wind-hub,options=rbind:rw",
                    *role_env,
                    args.image,
                    ident,
                    "python",
                    "-m",
                    *_commands(name, service_files),
                ]
            )
            # ctr creates/starts the initial task; subsequent restart and
            # stopped-task recreation are managed by the production CLI.
            result.append([*base, "tasks", "start", "--null-io", "--detach", ident])
        else:
            result.append(
                [
                    str(args.isula),
                    "create",
                    "--name",
                    ident,
                    "--network",
                    "host",
                    "--user",
                    f"{args.uid}:{args.gid}",
                    "-v",
                    f"{root}:/etc/wind-hub:rw",
                    *role_env,
                    args.image,
                    *_commands(name, service_files),
                ]
            )
            # iSulad initial create leaves the container stopped: use
            # 'wind-hub service start <name>' for lifecycle management.
    return result


def _created_target(args: argparse.Namespace, command: list[str]) -> str | None:
    if args.runtime == "containerd":
        if "containers" not in command:
            return None
        index = command.index("containers")
        if command[index : index + 2] != ["containers", "create"]:
            return None
        return command[command.index(args.image) + 1]
    if command[1:2] == ["create"]:
        return command[command.index("--name") + 1]
    return None


def _rollback(args: argparse.Namespace, targets: list[str]) -> None:
    """只回滚本次已成功 create 的精确 ID，绝不清扫同名前缀或既有资源。"""
    failures: list[str] = []
    for ident in reversed(targets):
        if args.runtime == "containerd":
            base = [args.ctr, "--address", str(args.socket), "--namespace", args.namespace]
            for operation in (("tasks", "kill", "-s", "SIGKILL", ident), ("tasks", "rm", ident)):
                subprocess.run([*base, *operation], capture_output=True, timeout=30, check=False)
            completed = subprocess.run(
                [*base, "containers", "rm", ident],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        else:
            completed = subprocess.run(
                [str(args.isula), "rm", "-f", ident],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        if completed.returncode != 0:
            failures.append(f"{ident}: {completed.stderr[:300]}")
    if failures:
        raise RuntimeError("rollback incomplete: " + "; ".join(failures))


def _inspect_target(args: argparse.Namespace, ident: str) -> bool | None:
    """True if present, False only on explicit not-found, None if uncertain."""
    if args.runtime == "containerd":
        command = [
            args.ctr,
            "--address",
            str(args.socket),
            "--namespace",
            args.namespace,
            "containers",
            "info",
            ident,
        ]
    else:
        command = [str(args.isula), "inspect", ident]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    error = (result.stdout + result.stderr).lower()
    if any(fragment in error for fragment in ("not found", "no such container", "does not exist")):
        return False
    return None


def _apply(args: argparse.Namespace, commands: list[list[str]]) -> None:
    created: list[str] = []
    uncertain: str | None = None
    try:
        for command in commands:
            target = _created_target(args, command)
            if target is not None:
                presence = _inspect_target(args, target)
                if presence is not False:
                    raise RuntimeError(
                        f"refusing create for existing or unverified target {target}: "
                        f"present={presence}"
                    )
            try:
                subprocess.run(command, check=True, timeout=120)
            except (
                subprocess.TimeoutExpired,
                subprocess.CalledProcessError,
                KeyboardInterrupt,
                OSError,
            ):
                if target is not None:
                    uncertain = target
                raise
            if target is not None:
                created.append(target)
    except BaseException as exc:
        _rollback(args, created)
        if uncertain is not None:
            presence = _inspect_target(args, uncertain)
            raise RuntimeError(
                f"OCI create result UNKNOWN for {uncertain} (present={presence}); "
                "ownership cannot be established, target was NOT deleted; "
                "manual runtime reconciliation required"
            ) from exc
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("containerd", "isulad"), required=True)
    parser.add_argument("--service", choices=("all", *SERVICE_NAMES), default="all")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--deployment", default=DEFAULT_DEPLOYMENT_PATH)
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
            _apply(args, commands)
        return 0
    except KeyboardInterrupt:
        parser.exit(130, "OCI provision interrupted; owned resources rolled back\n")
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        parser.exit(2, f"OCI provision failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
