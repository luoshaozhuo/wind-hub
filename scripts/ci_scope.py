"""按变更路径判断是否需要产品代码质量门禁。"""

from __future__ import annotations

import argparse
from pathlib import Path


PRODUCT_PREFIXES = ("src/", "tests/", "configs/")
PRODUCT_FILES = {"pyproject.toml", "poetry.lock"}
FRONTEND_PREFIX = "src/wind-hub-admin/"
TOOLING_PREFIXES = ("scripts/", ".github/", ".claude/", ".codex/", ".agents/", ".agent/")
GOVERNANCE_PREFIXES = ("ai_shared/", "docs/")
GOVERNANCE_FILES = {"CLAUDE.md", "AGENTS.md", "README.md", ".gitignore", ".env.local.example"}


def classify(paths: list[str]) -> dict[str, bool]:
    """返回产品代码、后端、前端、工具和治理范围。"""
    normalized = [path.strip().replace("\\", "/") for path in paths if path.strip()]

    frontend = any(path.startswith(FRONTEND_PREFIX) for path in normalized)
    backend = any(
        path in PRODUCT_FILES
        or path.startswith("tests/")
        or path.startswith("configs/")
        or (path.startswith("src/") and not path.startswith(FRONTEND_PREFIX))
        for path in normalized
    )
    product = backend or frontend
    tooling = any(path.startswith(TOOLING_PREFIXES) for path in normalized)
    governance = any(
        path in GOVERNANCE_FILES or path.startswith(GOVERNANCE_PREFIXES)
        for path in normalized
    )

    return {
        "product": product,
        "backend": backend,
        "frontend": frontend,
        "tooling": tooling,
        "governance": governance,
    }


def _write_github_output(path: Path, result: dict[str, bool]) -> None:
    """写 GitHub Actions step output。"""
    with path.open("a", encoding="utf-8") as handle:
        for key, value in result.items():
            handle.write(f"{key}={'true' if value else 'false'}\n")


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--file-list", type=Path)
    parser.add_argument("--github-output", type=Path)
    return parser


def main() -> int:
    """分类变更范围并输出结果。"""
    args = build_parser().parse_args()
    paths = list(args.paths)
    if args.file_list:
        paths.extend(args.file_list.read_text(encoding="utf-8").splitlines())

    result = classify(paths)
    for key, value in result.items():
        print(f"{key}={'true' if value else 'false'}")

    if args.github_output:
        _write_github_output(args.github_output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
