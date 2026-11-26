"""复制已构建 DCEMS Web 并修复本机测试环境的历史 WebSocket 地址。"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RELEASE_WEB = ROOT / "third_party" / "dc-ems" / "app" / "web"
BUNDLE_NAME = "app.4ad853ce.js"

LEGACY = (
    'baseUrl:"wss://".concat(location.host.includes("localhost")?'
    '"10.0.160.86:443":location.host.includes("127.0.0.1")?'
    '"10.0.0.2:443":location.host,"/api/ws")'
)
LOCAL = 'baseUrl:(location.protocol==="https:"?"wss://":"ws://")' '+location.host+"/api/ws"'


def stage_web(source: Path, destination: Path) -> Path:
    """准确匹配构建版本后才修复，不修改 third_party 原文件。"""
    source = source.resolve(strict=True)
    if destination.exists():
        raise FileExistsError(destination)
    bundle = source / "static" / "js" / BUNDLE_NAME
    if not bundle.is_file():
        raise FileNotFoundError(bundle)
    original = bundle.read_text(encoding="utf-8")
    if original.count(LEGACY) != 1:
        raise ValueError("Web build differs; do not rewrite an unknown JS bundle")
    shutil.copytree(source, destination)
    changed = destination / "static" / "js" / BUNDLE_NAME
    changed.write_text(original.replace(LEGACY, LOCAL), encoding="utf-8")
    return destination


def main() -> None:
    """准备独立 Web 静态文件供 nginx.smoke.conf 提供。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=RELEASE_WEB)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(stage_web(args.source, args.output))


if __name__ == "__main__":
    main()
