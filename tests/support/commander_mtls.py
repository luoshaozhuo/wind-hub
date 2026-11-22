"""真实 Commander mTLS 系统测试配置：复用正式出厂 Schema 和测试 PKI。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import yaml

from tests.commander.grpc_harness import IDENTITIES, Pki

_SHIPPED = Path(__file__).resolve().parents[2] / "configs" / "services" / "service_commander_v1.4.0.yaml"


def write_commander_service(
    root: Path,
    config_tree: Path,
    pki: Pki,
    *,
    grpc_port: int,
) -> Path:
    """生成可用正式 CLI 启动的 Commander 服务文件，证书不写入仓库。"""
    root = root.resolve()
    tree = config_tree.resolve(strict=True)
    if not tree.is_relative_to(root):
        raise ValueError("Commander shared configuration must reside under the test root")
    devices = tree / "instances" / "device_system_v1.0.0.yaml"
    templates = tree / "templates"
    if not devices.is_file() or not templates.is_dir():
        raise ValueError("Commander shared instances/templates are unavailable")
    document = yaml.safe_load(_SHIPPED.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("invalid shipped Commander service settings")
    document["config_sources"] = {"instances": {"devices": "system"}}
    document["grpc"] = {
        "listen_host": "127.0.0.1",
        "listen_port": grpc_port,
        "max_active_rpcs": 256,
    }
    document["security"] = {
        "ca_cert": str(pki.tmp / "ca.crt"),
        "server_cert": str(pki.tmp / "server.crt"),
        "server_key": str(pki.tmp / "server.key"),
        "callers": [
            {
                "caller_id": IDENTITIES["ops"][1],
                "certificate_identity": IDENTITIES["ops"][0],
                "permissions": ["query", "control", "reload", "recover"],
            }
        ],
    }
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    document["repository"]["sqlite_path"] = str(state_dir / "commander.sqlite3")
    document["repository"]["max_storage_bytes"] = 1073741824
    document["repository"]["reserved_storage_bytes"] = 67108864
    document["audit"]["nats_servers"] = ["nats://127.0.0.1:1"]
    document["audit"]["stream"] = f"CMD_E2E_{uuid4().hex[:12]}"
    document["audit"]["subject"] = f"commander.e2e.{uuid4().hex[:8]}"
    service_dir = tree / "services"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "service_commander_v1.4.0.yaml"
    service_file.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    deployment = tree / "deployments" / "deployment_commander-system_v1.0.0.yaml"
    deployment.parent.mkdir(parents=True, exist_ok=True)
    deployment.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "file_version": "1.0.0",
                "deployment_id": "commander-system",
                "services": {
                    "commander": {"config_id": "commander", "file_version": "1.4.0"}
                },
                "instances": {
                    "device": {"system": {"file_version": "1.0.0"}}
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return service_file


def commander_client_env(pki: Pki) -> dict[str, str]:
    """仅将证书 pathname 传给 Web 子进程，避免隐式明文回退。"""
    return {
        "WIND_HUB_COMMANDER_CA_CERT": str(pki.tmp / "ca.crt"),
        "WIND_HUB_COMMANDER_CLIENT_CERT": str(pki.tmp / "ops.crt"),
        "WIND_HUB_COMMANDER_CLIENT_KEY": str(pki.tmp / "ops.key"),
    }
