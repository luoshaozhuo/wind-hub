"""按既有 CLI 配置契约生成十设备随机联测的进程管理配置。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parents[3]


def prepare_cli(config_root: Path, runtime_dir: Path) -> Path:
    """使用两个真正的 CLI 受管进程，不通过测试内部对象启动服务。"""
    config_root = config_root.resolve(strict=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    service_names = {
        "gateway": (
            "industrial_gateway.main",
            config_root / "services" / "service_gateway-dcems-smoke_v1.0.0.yaml",
            "127.0.0.1:52073",
            "WIND_HUB_DCE_GATEWAY_OPERATOR_TOKEN",
        ),
        "data-generator": (
            "data_generator.main",
            config_root / "services" / "service_data-generator-dcems-smoke_v1.0.0.yaml",
            "127.0.0.1:52061",
            "WIND_HUB_DCE_GENERATOR_CLI_TOKEN",
        ),
    }
    services = {}
    for name, (module, path, endpoint, token_env) in service_names.items():
        if not path.is_file():
            raise FileNotFoundError(path)
        argv = [sys.executable, "-m", module, "--config", str(path)]
        if name == "gateway":
            argv.extend(["--project-root", str(config_root)])
        services[name] = {
            "token_env": token_env,
            "grpc_endpoint": endpoint,
            "manager": "process",
            "target": name,
            "process": {
                "argv": argv,
                "cwd": str(PROJECT),
                "state_dir": str(runtime_dir / "cli-processes"),
                "log_file": str(runtime_dir / f"{name}.log"),
            },
        }
    document = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "service_id": "cli-dcems-smoke",
        "cli": {
            "prompt": "dcems-smoke> ",
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
    """从已装配的配置根生成真实 CLI 受管进程文件。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    print(prepare_cli(args.config_root, args.runtime_dir))


if __name__ == "__main__":
    main()
