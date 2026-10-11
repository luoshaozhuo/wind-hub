"""生成或验证 Web Server OpenAPI 契约，不依赖旧 Admin 生成物。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "src" / "web_admin" / "src" / "api" / "generated" / "openapi.json"


def render() -> str:
    """从新 FastAPI app 直接读取接口定义。"""
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from web_server.app import create_app

    return json.dumps(create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    schema = json.loads(render())
    required = {
        "/api/v1/collector/latest-values",
        "/api/v1/collector/tasks",
        "/api/v1/collector/sinks",
        "/api/v1/commander/commands",
        "/api/v1/config/prepare",
        "/api/v1/config/activate",
    }
    if args.check:
        missing = sorted(required - schema["paths"].keys())
        if missing:
            print(f"Missing routes: {missing}", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
