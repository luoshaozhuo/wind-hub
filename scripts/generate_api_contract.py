"""从当前 Web Server 应用直接生成 OpenAPI，禁止从已删除的旧 Server 复制契约。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "src" / "web_admin" / "src" / "api" / "generated" / "openapi.json"


def render() -> str:
    """输出可重复生成的 OpenAPI JSON，不启动 gRPC 服务。"""
    from web_server.app import create_app

    return json.dumps(
        create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generated = render()
    if args.check:
        schema = json.loads(generated)
        required = {"/api/v1/collector/latest-values", "/api/v1/commander/commands"}
        if not required.issubset(schema["paths"]):
            print("Required runtime API routes are missing", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
