"""从 FastAPI OpenAPI 生成前端 TypeScript 契约。

链路：``build_api().openapi()`` → ``generated/openapi.json`` →
``openapi-typescript`` → ``generated/schema.d.ts``。

默认直接重新生成；``--check`` 重新生成后通过 ``git diff --exit-code``
检测漂移（后端 DTO 变更但前端契约未更新时失败）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "src" / "wind-hub-admin"
GENERATED_DIR = FRONTEND_DIR / "src" / "api" / "generated"
OPENAPI_JSON = GENERATED_DIR / "openapi.json"
SCHEMA_TS = GENERATED_DIR / "schema.d.ts"


def export_openapi() -> None:
    """从 FastAPI 应用导出确定性 OpenAPI JSON。"""
    from wind_hub_server.adapter.inbound.webapi.app import build_api

    schema = build_api().openapi()
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    OPENAPI_JSON.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"openapi.json: {len(schema.get('paths', {}))} paths")


def generate_typescript() -> None:
    """用 openapi-typescript 从 OpenAPI JSON 生成 TypeScript 契约。"""
    npx = shutil.which("npx")
    if npx is None:
        print("npx not found; install Node.js first", file=sys.stderr)
        raise SystemExit(2)
    result = subprocess.run(
        [
            npx,
            "openapi-typescript",
            str(OPENAPI_JSON),
            "-o",
            str(SCHEMA_TS),
        ],
        cwd=FRONTEND_DIR,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    print(f"schema.d.ts: {SCHEMA_TS.relative_to(REPO_ROOT)}")


def check_drift() -> bool:
    """生成物与工作区一致则通过，否则报告漂移。"""
    result = subprocess.run(
        ["git", "diff", "--exit-code", "--", str(GENERATED_DIR.relative_to(REPO_ROOT))],
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode == 0:
        print("DRIFT CHECK: PASS")
        return True
    print(
        "DRIFT CHECK: FAIL — generated API contract is stale; "
        "run `python3 scripts/generate_api_contract.py` and commit the result",
        file=sys.stderr,
    )
    return False


def main() -> int:
    """生成契约，按需执行漂移检查。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="检测生成物漂移")
    args = parser.parse_args()

    export_openapi()
    generate_typescript()
    if args.check:
        return 0 if check_drift() else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
