"""生成完整测量链路的 CLI 进程编排文件。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from tests.third_party.dc_ems.prepare_cli import PROJECT


def prepare_cli(config_root: Path, runtime_dir: Path) -> Path:
    """将双 Gateway、DG、Collector、Sink 绑定到正式 CLI。"""
    config_root = config_root.resolve(strict=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    root = config_root.parent
    process_definitions: dict[str, tuple[str, str, list[str], str | None]] = {
        "gateway": (
            "industrial_gateway.main",
            "service_gateway-dcems-source_v1.0.0.yaml",
            ["--project-root", str(config_root)],
            "WIND_HUB_DCE_GATEWAY_OPERATOR_TOKEN",
        ),
        "gateway-target": (
            "industrial_gateway.main",
            "service_gateway-dcems-target_v1.0.0.yaml",
            ["--project-root", str(config_root)],
            "WIND_HUB_DCE_TARGET_OPERATOR_TOKEN",
        ),
        "data-generator": (
            "data_generator.main",
            "service_data-generator-dcems-measurement_v1.0.0.yaml",
            [],
            "WIND_HUB_DCE_GENERATOR_CLI_TOKEN",
        ),
        "collector-realtime": (
            "collector.main",
            "service_collector-dcems-realtime_v2.0.0.yaml",
            [
                "--state-directory",
                str(runtime_dir / "collector"),
                "--collector-id",
                "collector-dcems-realtime",
                "--grpc-host",
                "127.0.0.1",
                "--grpc-port",
                "52051",
            ],
            None,
        ),
        "sink": (
            "sink_service.main",
            "service_sink-dcems-measurement_v2.0.0.yaml",
            [],
            "WIND_HUB_SINK_CLI_TOKEN",
        ),
    }
    endpoints = {
        "gateway": "127.0.0.1:52073",
        "gateway-target": "127.0.0.1:52074",
        "data-generator": "127.0.0.1:52061",
        "collector-realtime": "127.0.0.1:52051",
        "sink": "127.0.0.1:52053",
    }
    services: dict[str, Any] = {}
    for name, (module, filename, additional, token_env) in process_definitions.items():
        service = config_root / "services" / filename
        if not service.is_file():
            raise FileNotFoundError(service)
        argv = [sys.executable, "-m", module]
        if name == "sink":
            argv.extend(
                [
                    "--config-root",
                    str(root),
                    "--service-file",
                    f"configs/services/{filename}",
                ]
            )
        else:
            argv.extend(["--config", str(service), *additional])
        entry: dict[str, Any] = {
            "grpc_endpoint": endpoints[name],
            "manager": "process",
            "target": name,
            "process": {
                "argv": argv,
                "cwd": str(PROJECT),
                "state_dir": str(runtime_dir / "cli-processes"),
                "log_file": str(runtime_dir / f"{name}.log"),
            },
        }
        if token_env is not None:
            entry["token_env"] = token_env
        services[name] = entry
    document = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "service_id": "cli-dcems-measurement",
        "cli": {
            "prompt": "dcems-measurement> ",
            "output_format": "json",
            "timeout_seconds": 30,
            "history": False,
            "max_display_rows": 1000,
            "poll_interval_ms": 1000,
            "poll_reconnect_attempts": 3,
            "snapshot_max_bytes": 67108864,
            "diff_max_file_bytes": 1048576,
            "diff_max_output_lines": 10000,
        },
        "services": services,
        "network_probe": {
            "max_hosts": 1024,
            "max_concurrency": 64,
            "timeout_seconds": 1,
            "industrial_tcp_ports": [502],
        },
    }
    output = runtime_dir / "cli.yaml"
    if output.exists():
        raise FileExistsError(output)
    output.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return output


def main() -> None:
    """在临时根下写出 CLI 服务映射。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    print(prepare_cli(args.config_root, args.runtime_dir))


if __name__ == "__main__":
    main()
